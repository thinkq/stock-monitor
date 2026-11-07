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
    assert state.observe('TESTUSDT', obs, now, Config()).alert_id == 1
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


def advance(obs, atr_units, cfg=Config(), minutes=30):
    """构造"同向继续推进"的观测：价格按 ATR 倍数上移，换一根已收盘 K 线。"""
    signal = replace(obs.signal,
                     price=obs.signal.price + atr_units * obs.signal.atr,
                     atr_units=obs.signal.atr_units + atr_units)
    return replace(obs, signal=signal, quiet=False,
                   closed_time=obs.closed_time + cfg.interval_ms), minutes * 60_000


def test_no_escalation_when_first_alert_was_suppressed():
    """首报被冷却压住时，绝不能用上一波的起点算出"升级"。"""
    obs, now = observation()
    state = State(':memory:')
    cfg = Config()
    state.observe('TESTUSDT', obs, now, cfg)                 # 第一波：正常首报
    quiet_reset(state, obs, now, cfg)                        # 两轮平静 → 结束
    assert state.db.execute("select announced_atr from episodes where direction='up'").fetchone()[0] == 0
    fresh, _ = advance(obs, 0.0)                             # 第二波首报（60 分钟内，必被冷却）
    assert state.observe('TESTUSDT', fresh, now + 2 * cfg.interval_ms, cfg) is None
    far, delta = advance(obs, 3.0)
    assert state.observe('TESTUSDT', far, now + 2 * cfg.interval_ms + delta, cfg) is None
    assert state.db.execute("select count(*) from alerts where kind='escalation'").fetchone()[0] == 0
    state.close()


def test_fast_confirms_across_a_two_minute_gap():
    """cron 实际约 120 秒一轮；确认流程不能因此被打断（以前 90 秒窗口会让它永远确认不了）。"""
    obs, now = observation()
    fast = replace(obs, signal=replace(obs.signal, kind='fast', source='forming'))
    cfg = Config()
    state = State(':memory:')
    assert state.observe('TESTUSDT', fast, now, cfg) is None          # 首次候选
    assert state.observe('TESTUSDT', fast, now + 120_000, cfg) is not None   # 120 秒后确认
    state.close()


def test_maybe_prune_runs_once_per_day(tmp_path):
    state = State(tmp_path / 'prune.db')
    obs, now = observation()
    state.observe('TESTUSDT', obs, now, Config())
    state.db.execute("UPDATE alerts SET day='2020-01-01'")
    assert state.maybe_prune('2026-11-07', 180) == 1
    assert state.db.execute('select count(*) from alerts').fetchone()[0] == 0
    assert state.maybe_prune('2026-11-07', 180) == 0      # 同一天不重复扫描
    assert state.maybe_prune('2026-11-08', 180) == 0      # 换天再试，但没有新数据
    assert state.maybe_prune('2026-11-09', 0) == 0        # 0 = 关闭
    state.close()


def test_escalation_fires_after_threshold_and_gap():
    obs, now = observation()
    state = State(':memory:')
    cfg = Config()
    first = state.observe('TESTUSDT', obs, now, cfg)
    assert first.signal.kind == 'sustained'
    later, delta = advance(obs, 1.6)
    escalated = state.observe('TESTUSDT', later, now + delta, cfg)
    assert escalated is not None
    assert escalated.signal.kind == 'escalation'
    assert escalated.signal.escalation_from == first.signal.atr_units
    assert state.db.execute("select count(*) from alerts where kind='escalation'").fetchone()[0] == 1
    state.close()


def test_escalation_waits_for_the_gap():
    obs, now = observation()
    state = State(':memory:')
    cfg = Config()
    state.observe('TESTUSDT', obs, now, cfg)
    later, _ = advance(obs, 1.6)
    assert state.observe('TESTUSDT', later, now + 20 * 60_000, cfg) is None
    assert state.observe('TESTUSDT', later, now + 30 * 60_000, cfg) is not None
    state.close()


def test_escalation_needs_a_bigger_move():
    obs, now = observation()
    state = State(':memory:')
    cfg = Config()
    state.observe('TESTUSDT', obs, now, cfg)
    later, delta = advance(obs, 1.0)
    assert state.observe('TESTUSDT', later, now + delta, cfg) is None
    state.close()


def test_escalation_ignores_retracement():
    obs, now = observation()
    state = State(':memory:')
    cfg = Config()
    state.observe('TESTUSDT', obs, now, cfg)
    later = replace(obs, signal=replace(obs.signal, price=obs.signal.price - 2 * obs.signal.atr),
                    quiet=False, closed_time=obs.closed_time + cfg.interval_ms)
    assert state.observe('TESTUSDT', later, now + 3600_000, cfg) is None
    state.close()


def test_escalation_can_be_disabled():
    obs, now = observation()
    state = State(':memory:')
    cfg = replace(Config(), escalation_atr=0.0)
    state.observe('TESTUSDT', obs, now, cfg)
    later, delta = advance(obs, 3.0)
    assert state.observe('TESTUSDT', later, now + delta, cfg) is None
    assert state.db.execute("select count(*) from alerts where kind='escalation'").fetchone()[0] == 0
    state.close()


def test_escalation_has_its_own_daily_quota():
    """首报额度用尽不影响升级（它是同一波行情的延续），升级另有自己的上限。"""
    obs, now = observation()
    state = State(':memory:')
    cfg = replace(Config(), daily_limit=1, escalation_daily_limit=1)
    assert state.observe('TESTUSDT', obs, now, cfg) is not None      # 首报占满唯一名额
    later, delta = advance(obs, 2.0)
    escalated = state.observe('TESTUSDT', later, now + delta, cfg)
    assert escalated is not None and escalated.signal.kind == 'escalation'
    further, delta2 = advance(obs, 4.0, minutes=60)
    assert state.observe('TESTUSDT', further, now + delta2, cfg) is None   # 升级额度 1 已用完
    assert state.db.execute("select count(*) from alerts where kind='escalation'").fetchone()[0] == 1
    state.close()


def test_escalation_state_clears_after_reset():
    obs, now = observation()
    state = State(':memory:')
    cfg = replace(Config(), cooldown_seconds=0)
    state.observe('TESTUSDT', obs, now, cfg)
    quiet_reset(state, obs, now, cfg)
    later, _ = advance(obs, 3.0)
    fresh = state.observe('TESTUSDT', later, now + 3 * cfg.interval_ms, cfg)
    assert fresh.signal.kind == 'sustained'      # 新的一波，重新算首报
    assert state.db.execute("select announced_atr from episodes where direction='up'").fetchone()[0] == fresh.signal.atr_units
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
    minutes['quote_volume'] = 10. * minutes['close']
    minutes['trades'] = 10
    minutes['taker_buy_volume'] = 5.
    minutes['taker_buy_quote_volume'] = 5. * minutes['close']
    minutes['close_time'] = minutes.time + 59_999
    state = State(':memory:')
    cfg = Config(cooldown_seconds=0)
    alerts = []
    for i in range(1500, len(prices)):
        prefix = minutes.iloc[:i + 1]
        bars = prefix.groupby(prefix.time // 900_000 * 900_000).agg(
            open=('open', 'first'), high=('high', 'max'), low=('low', 'min'),
            close=('close', 'last'), volume=('volume', 'sum'), trades=('trades', 'sum'),
            quote_volume=('quote_volume', 'sum'), taker_buy_volume=('taker_buy_volume', 'sum'),
            taker_buy_quote_volume=('taker_buy_quote_volume', 'sum')).reset_index()
        bars['close_time'] = bars.time + 899_999
        now = int(prefix.time.iloc[-1]) + 30_000
        obs = detect('TESTUSDT', bars[COLUMNS].tail(100), prefix[COLUMNS].tail(65), now, cfg)
        reserved = state.observe('TESTUSDT', obs, now, cfg)
        if reserved:
            alerts.append((i, reserved.signal.direction, reserved.signal.kind))
    firsts = [(i, direction) for i, direction, kind in alerts if kind != 'escalation']
    assert [direction for _, direction in firsts] == ['up', 'down']
    assert firsts[0][0] <= 1530
    assert firsts[1][0] <= 1720
    # 连续 90 分钟单边上涨里，位移会持续扩大，必须出现升级提醒
    assert [kind for _, _, kind in alerts].count('escalation') >= 1
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
    # 间隔超过标称扫描周期的 3 倍（180 秒）才算断档：这里隔了 240 秒
    assert state.observe('TESTUSDT', down, now + 300_000, cfg) is None
    assert state.observe('TESTUSDT', down, now + 360_000, cfg)
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
