from dataclasses import replace

import numpy as np
import pytest
import talib

from market_monitor.config import Config, Detection, load
from market_monitor.demo import scenario
from market_monitor.detection import detect, efficiency, indicators
from market_monitor.market import MarketError


@pytest.mark.parametrize('tail,direction', [((101, 102.1, 103.2), 'up'), ((99, 97.9, 96.8), 'down')])
def test_sustained(tail, direction):
    b, m, now = scenario(tail)
    signal = detect('TESTUSDT', b, m, now, Config()).signal
    assert signal.direction == direction
    assert signal.kind == 'sustained'
    assert signal.source == 'closed'
    assert signal.er > .99


@pytest.mark.parametrize('tail', [(100.02, 99.98, 100.01), (100.1, 100.2, 100.3), (101, 99, 100)])
def test_no_noise(tail):
    assert detect('TESTUSDT', *scenario(tail), Config()).signal is None


@pytest.mark.parametrize('price,direction', [(102.5, 'up'), (97.5, 'down'), (150, 'up'), (50, 'down')])
def test_forming_fast_and_extreme(price, direction):
    b, m, now = scenario((100, 100, 100), price)
    signal = detect('TESTUSDT', b, m, now, Config()).signal
    assert signal.direction == direction
    assert signal.kind == 'fast'
    assert signal.source == 'forming'
    assert b.close_time.iloc[-1] > now


def test_fast_reversal_overrides_closed_uptrend():
    signal = detect('TESTUSDT', *scenario((101, 102.2, 103.5), 98), Config()).signal
    assert (signal.direction, signal.kind) == ('down', 'fast')


def test_small_pullback_allowed():
    signal = detect('TESTUSDT', *scenario((101.2, 101.17, 102.5)), Config()).signal
    assert signal.kind == 'sustained'


def test_disallowed_direction():
    assert detect('TESTUSDT', *scenario(), Config(directions=['down'])).signal is None


def test_sensitivity():
    cfg = Config(detection=Detection(sensitivity=10))
    assert detect('TESTUSDT', *scenario(), cfg).signal is None


@pytest.mark.parametrize('damage', ['missing', 'stale', 'future', 'nan', 'invalid', 'zero', 'duplicate', 'warmup'])
def test_bad_data_rejected(damage):
    b, m, now = scenario()
    if damage == 'missing': b = b.drop(index=len(b) - 3)
    if damage == 'stale': now += 900_000
    if damage == 'future': now -= 900_000
    if damage == 'nan': b.loc[10, 'close'] = np.nan
    if damage == 'invalid': b.loc[10, 'low'] = 200
    if damage == 'zero': b.loc[10, 'close'] = 0
    if damage == 'duplicate': b.loc[10, 'time'] = b.loc[9, 'time']
    if damage == 'warmup': b = b.tail(20)
    with pytest.raises(MarketError): detect('TESTUSDT', b, m, now, Config())


def test_missing_minute_rejected():
    b, m, now = scenario()
    with pytest.raises(MarketError): detect('TESTUSDT', b, m.drop(index=30), now, Config())


def test_atr_reference_and_no_current_candle_contamination():
    b, _, now = scenario()
    closed = b[b.close_time < now]
    h, l, c = (closed[k].to_numpy(dtype=float) for k in ('high', 'low', 'close'))
    atr, adx, plus, minus = indicators(closed, Config())
    assert atr == pytest.approx(talib.ATR(h[:-4], l[:-4], c[:-4], 14)[-1])
    assert adx == pytest.approx(talib.ADX(h, l, c, 14)[-1])
    assert plus == pytest.approx(talib.PLUS_DI(h, l, c, 14)[-1])
    assert minus == pytest.approx(talib.MINUS_DI(h, l, c, 14)[-1])
    # Stable pre-shock bars have known true range 0.21; ATR is independently bounded.
    assert .20 < atr < .22


def test_er_known_values():
    assert efficiency([1, 2, 3]) == 1
    assert efficiency([1, 2, 1]) == 0
    assert efficiency([1, 1, 1]) == 0
    assert efficiency([100, 102, 101, 103]) == pytest.approx(3 / 5)


def test_rolling_window_uses_minute_prices():
    b, m, now = scenario()
    s = detect('TESTUSDT', b, m, now, Config()).signal
    for window in (15, 30, 45, 60):
        past = m[m.close_time <= now - window * 60_000].iloc[-1]
        assert s.changes[window] == pytest.approx((m.close.iloc[-1] / past.close - 1) * 100)
        assert 0 <= now - window * 60_000 - past.close_time < 60_000


def test_sample_config():
    cfg = load('config.toml')
    assert cfg.symbols and len(cfg.symbols) == len(set(cfg.symbols))


@pytest.mark.parametrize('kwargs', [dict(interval='7m'), dict(scan_seconds=0), dict(directions=['long']),
                                  dict(history_bars=20), dict(symbols=['BTCUSDT', 'BTCUSDT']),
                                  dict(detection=Detection(er_min=2)), dict(detection=Detection(atr_period=1)),
                                  dict(detection=Detection(fast_er_min=1.1)), dict(fast_confirmation_seconds=-1)])
def test_config_validation(kwargs):
    with pytest.raises(ValueError): Config(**kwargs)


def test_current_reversal_wins_even_if_30m_still_positive():
    b, m, now = scenario((103, 106, 109), 106.8)
    signal = detect('TESTUSDT', b, m, now, Config()).signal
    assert signal.changes[30] > 0
    assert (signal.direction, signal.kind) == ('down', 'fast')


def test_closed_trend_not_confirmed_after_material_retracement():
    signal = detect('TESTUSDT', *scenario((101, 102.1, 103.2), 102.3), Config()).signal
    assert signal is None or signal.kind != 'sustained'


@pytest.mark.parametrize('interval,ms', [('1m', 60_000), ('5m', 300_000), ('30m', 1_800_000)])
def test_configurable_intervals(interval, ms):
    # 1m scenarios need at least 65 minutes; the synthetic main history provides this.
    signal = detect('TESTUSDT', *scenario(interval_ms=ms), Config(interval=interval)).signal
    assert signal and signal.direction == 'up'


@pytest.mark.parametrize('direction', [1, -1])
def test_fast_rejects_large_but_choppy_minute_path(direction):
    b, m, now = scenario((100, 100, 100), 100 + direction * 2.5)
    indexes = m.index[m.time >= b.time.iloc[-1]]
    values = 100 + direction * np.array([1.8, -.8, 2.1, -.5, 2.3, 0, 2.4, 2.5])
    assert len(indexes) == len(values)
    m.loc[indexes, 'close'] = values
    m.loc[indexes, 'high'] = m.loc[indexes, ['open', 'close']].max(axis=1) + .01
    m.loc[indexes, 'low'] = m.loc[indexes, ['open', 'close']].min(axis=1) - .01
    # Recreate the previous ER-unfiltered behavior while keeping the new ATR threshold.
    old = Config(detection=Detection(fast_er_min=.001))
    assert detect('TESTUSDT', b, m, now, old).signal is not None
    assert detect('TESTUSDT', b, m, now, Config()).signal is None


def test_fast_requires_two_atr_even_with_clean_path():
    b, m, now = scenario((100, 100, 100), 101.8)
    b['high'] = b[['open', 'close']].max(axis=1) + .49
    b['low'] = b[['open', 'close']].min(axis=1) - .49
    old = Config(detection=Detection(fast_atr=1.5))
    assert detect('TESTUSDT', b, m, now, old).signal.kind == 'fast'
    assert detect('TESTUSDT', b, m, now, Config()).signal is None
