from dataclasses import replace

from market_monitor.config import Config
from market_monitor.demo import scenario
from market_monitor.detection import Observation, detect
from market_monitor.state import State


def observation():
    b, m, now = scenario()
    return detect('TESTUSDT', b, m, now, Config()), now


def test_one_episode_and_restart(tmp_path):
    path = tmp_path / 'state.db'
    obs, now = observation()
    state = State(path)
    assert state.observe('TESTUSDT', obs, now, Config()) == 1
    for minute in range(60):
        assert state.observe('TESTUSDT', obs, now + minute * 60_000, Config()) is None
    state.close()
    state = State(path)
    assert state.observe('TESTUSDT', obs, now + 3600_000, Config()) is None
    assert state.db.execute('select count(*) from alerts').fetchone()[0] == 1
    state.close()


def test_upgrade_silent():
    obs, now = observation()
    fast = replace(obs, signal=replace(obs.signal, kind='fast', source='forming'))
    state = State(':memory:')
    assert state.observe('TESTUSDT', fast, now, Config()) is None
    assert state.observe('TESTUSDT', fast, now + 60_000, Config())
    assert state.observe('TESTUSDT', obs, now + 60_000, Config()) is None
    assert state.db.execute("select kind from episodes where direction='up'").fetchone()[0] == 'sustained'
    state.close()


def quiet_reset(state, obs, now, cfg):
    for i in (1, 2):
        q = Observation(None, True, obs.closed_time + i * cfg.interval_ms)
        state.observe('TESTUSDT', q, now + i * cfg.interval_ms, cfg)


def test_reset_requires_distinct_quiet_closed_candles():
    obs, now = observation()
    cfg = Config(cooldown_seconds=0)
    state = State(':memory:')
    state.observe('TESTUSDT', obs, now, cfg)
    q = Observation(None, True, obs.closed_time + cfg.interval_ms)
    for _ in range(60): state.observe('TESTUSDT', q, now + cfg.interval_ms, cfg)
    assert state.db.execute("select active from episodes where direction='up'").fetchone()[0] == 1
    quiet_reset(state, obs, now, cfg)
    assert state.observe('TESTUSDT', replace(obs, closed_time=q.closed_time + 2 * cfg.interval_ms), now + 3 * cfg.interval_ms, cfg)
    state.close()


def test_cooldown_and_limit_suppression_not_delayed_spam():
    obs, now = observation()
    cfg = Config(cooldown_seconds=7200)
    state = State(':memory:')
    state.observe('TESTUSDT', obs, now, cfg)
    quiet_reset(state, obs, now, cfg)
    assert state.observe('TESTUSDT', obs, now + 1800_000, cfg) is None
    assert state.observe('TESTUSDT', obs, now + 8000_000, cfg) is None
    assert state.db.execute("select detail from alerts where status='suppressed'").fetchone()[0] == 'cooldown'
    state.close()
    state = State(':memory:')
    cfg = Config(daily_limit=1)
    assert state.observe('TESTUSDT', obs, now, cfg)
    assert state.observe('SECONDUSDT', replace(obs, signal=replace(obs.signal, symbol='SECONDUSDT')), now, cfg) is None
    assert state.db.execute("select detail from alerts where status='suppressed'").fetchone()[0] == 'daily_limit'
    state.close()


def test_opposite_direction_independent():
    obs, now = observation()
    state = State(':memory:')
    assert state.observe('TESTUSDT', obs, now, Config())
    down = replace(obs, signal=replace(obs.signal, direction='down'))
    assert state.observe('TESTUSDT', down, now, Config())
    assert state.observe('TESTUSDT', obs, now + 1000, Config()) is None
    state.close()


def test_data_gap_cannot_accumulate_quiet_candles():
    obs, now = observation()
    cfg = Config()
    state = State(':memory:')
    state.observe('TESTUSDT', obs, now, cfg)
    for i in (1, 8):
        state.observe('TESTUSDT', Observation(None, True, obs.closed_time + i * cfg.interval_ms), now + i * cfg.interval_ms, cfg)
    assert state.db.execute("select active from episodes where direction='up'").fetchone()[0] == 1
    state.close()


def test_day_rollover_uses_config_timezone():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    obs, _ = observation()
    now = int(datetime(2026, 11, 5, 23, 59, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1000)
    cfg = Config(daily_limit=1)
    state = State(':memory:')
    assert state.observe('TESTUSDT', obs, now, cfg)
    other = replace(obs, signal=replace(obs.signal, symbol='SECONDUSDT'))
    assert state.observe('SECONDUSDT', other, now + 120_000, cfg)
    days = [r[0] for r in state.db.execute('select day from alerts order by id')]
    assert days == ['2026-11-05', '2026-11-06']
    state.close()


def test_two_connections_share_atomic_reservation(tmp_path):
    obs, now = observation()
    first = State(tmp_path / 'shared.db')
    second = State(tmp_path / 'shared.db')
    assert first.observe('TESTUSDT', obs, now, Config())
    assert second.observe('TESTUSDT', obs, now, Config()) is None
    first.close()
    second.close()


def test_forward_only_minute_replay_one_alert_per_episode():
    import numpy as np
    import pandas as pd
    from market_monitor.market import COLUMNS

    # 25h warmup, 90min rise, 100min quiet, 60min fall. Build each snapshot
    # from only the prefix available at that instant, including the partial 15m bar.
    prices = np.r_[np.full(1500, 100.), 100 * 1.001 ** np.arange(1, 91),
                   np.full(100, 100 * 1.001 ** 90),
                   100 * 1.001 ** 90 * .999 ** np.arange(1, 61)]
    start = 1_798_761_600_000 // 900_000 * 900_000
    minutes = pd.DataFrame({
        'time': start + np.arange(len(prices)) * 60_000,
        'open': np.r_[prices[0], prices[:-1]], 'close': prices,
    })
    minutes['high'] = minutes[['open', 'close']].max(axis=1) + .03
    minutes['low'] = minutes[['open', 'close']].min(axis=1) - .03
    minutes['volume'] = 10.
    minutes['trades'] = 10
    minutes['close_time'] = minutes.time + 59_999
    state = State(':memory:')
    cfg = Config(cooldown_seconds=0)
    alerts = []
    for i in range(1500, len(prices)):
        prefix = minutes.iloc[:i + 1]
        bars = prefix.groupby(prefix.time // 900_000 * 900_000).agg(
            open=('open', 'first'), high=('high', 'max'), low=('low', 'min'),
            close=('close', 'last'), volume=('volume', 'sum'), trades=('trades', 'sum')).reset_index()
        bars['close_time'] = bars.time + 899_999
        now = int(prefix.time.iloc[-1]) + 30_000
        obs = detect('TESTUSDT', bars[COLUMNS].tail(100), prefix[COLUMNS].tail(65), now, cfg)
        if state.observe('TESTUSDT', obs, now, cfg):
            alerts.append((i, obs.signal.direction))
    assert [direction for _, direction in alerts] == ['up', 'down']
    assert alerts[0][0] <= 1530
    assert alerts[1][0] <= 1720
    state.close()


def fast_observation():
    b, m, now = scenario((100, 100, 100), 102.5)
    return detect('TESTUSDT', b, m, now, Config()), now


def test_fast_waits_then_sends_once_after_restart(tmp_path):
    obs, now = fast_observation()
    path = tmp_path / 'confirm.db'
    cfg = Config()
    state = State(path)
    assert state.observe('TESTUSDT', obs, now, cfg) is None
    assert state.db.execute('select count(*) from alerts').fetchone()[0] == 0
    assert state.observe('TESTUSDT', obs, now + 1000, cfg) is None
    state.close()
    state = State(path)
    assert state.observe('TESTUSDT', obs, now + 60_000, cfg)
    assert state.observe('TESTUSDT', obs, now + 120_000, cfg) is None
    assert state.db.execute('select count(*) from pending_signals').fetchone()[0] == 0
    state.close()


def test_fast_disappearing_candidate_does_not_consume_episode_or_limit():
    obs, now = fast_observation()
    state = State(':memory:')
    cfg = Config()
    assert state.observe('TESTUSDT', obs, now, cfg) is None
    empty = replace(obs, signal=None)
    state.observe('TESTUSDT', empty, now + 60_000, cfg)
    assert state.observe('TESTUSDT', obs, now + 120_000, cfg) is None
    assert state.db.execute('select count(*) from alerts').fetchone()[0] == 0
    assert state.db.execute('select sum(active) from episodes').fetchone()[0] == 0
    assert state.observe('TESTUSDT', obs, now + 180_000, cfg)
    state.close()


def test_fast_direction_change_and_gap_restart_confirmation():
    obs, now = fast_observation()
    state = State(':memory:')
    cfg = Config()
    state.observe('TESTUSDT', obs, now, cfg)
    down = replace(obs, signal=replace(obs.signal, direction='down'))
    assert state.observe('TESTUSDT', down, now + 60_000, cfg) is None
    # Missing next scan invalidates confirmation, even though enough wall time passed.
    assert state.observe('TESTUSDT', down, now + 180_000, cfg) is None
    assert state.observe('TESTUSDT', down, now + 240_000, cfg)
    state.close()


def test_pending_fast_can_be_confirmed_immediately_by_closed_trend():
    obs, now = fast_observation()
    state = State(':memory:')
    cfg = Config()
    assert state.observe('TESTUSDT', obs, now, cfg) is None
    sustained = replace(obs, signal=replace(obs.signal, kind='sustained', source='closed'))
    assert state.observe('TESTUSDT', sustained, now + 1000, cfg)
    assert state.db.execute('select count(*) from pending_signals').fetchone()[0] == 0
    state.close()


def test_confirmation_can_be_disabled():
    obs, now = fast_observation()
    state = State(':memory:')
    assert state.observe('TESTUSDT', obs, now, Config(fast_confirmation_seconds=0))
    state.close()
