"""Pure detection: no I/O, clocks, state, or future candles."""
from dataclasses import dataclass

import numpy as np
import pandas as pd
import talib

from .config import Config
from .market import MarketError, validate_frame


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: str
    kind: str
    source: str
    price: float
    changes: dict[int, float]
    er: float
    atr_units: float
    adx: float
    plus_di: float
    minus_di: float
    detected_ms: int
    evidence_ms: int


@dataclass(frozen=True)
class Observation:
    signal: Signal | None
    quiet: bool
    closed_time: int


def efficiency(prices) -> float:
    """Kaufman ER; TA-Lib does not expose ER (KAMA is not ER)."""
    p = np.asarray(prices, dtype=float)
    distance = np.abs(np.diff(p)).sum()
    return float(abs(p[-1] - p[0]) / distance) if distance else 0.0


def indicators(closed: pd.DataFrame, config: Config):
    d = config.detection
    h, l, c = (closed[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    # Baseline precedes all 2–4-bar candidate windows; shocks cannot inflate their own baseline.
    atr = talib.ATR(h, l, c, timeperiod=d.atr_period)[-5]
    adx = talib.ADX(h, l, c, timeperiod=d.adx_period)[-1]
    plus = talib.PLUS_DI(h, l, c, timeperiod=d.adx_period)[-1]
    minus = talib.MINUS_DI(h, l, c, timeperiod=d.adx_period)[-1]
    if not np.isfinite([atr, adx, plus, minus]).all() or atr <= 0:
        raise MarketError("indicators not initialized or zero ATR")
    return float(atr), float(adx), float(plus), float(minus)


def rolling_changes(minutes, now_ms):
    price = float(minutes.close.iloc[-1])
    changes, bases = {}, {}
    for window in (15, 30, 45, 60):
        target = now_ms - window * 60_000
        reference = minutes.loc[minutes.close_time <= target]
        if reference.empty or target - int(reference.close_time.iloc[-1]) > 60_000:
            raise MarketError("rolling window reference missing")
        bases[window] = float(reference.close.iloc[-1])
        changes[window] = (price / bases[window] - 1) * 100
    return price, changes, bases


def detect(symbol, candles, minutes, now_ms, config: Config) -> Observation:
    validate_frame(candles, config.interval_ms, now_ms)
    validate_frame(minutes, 60_000, now_ms)
    closed = candles.loc[candles.close_time < now_ms]
    d = config.detection
    if len(closed) < max(d.atr_period + 6, 2 * d.adx_period + 5):
        raise MarketError("insufficient indicator history")
    atr, adx, plus, minus = indicators(closed, config)
    price, changes, bases = rolling_changes(minutes, now_ms)
    closes = closed.close.to_numpy(dtype=float)
    last = int(closed.time.iloc[-1])
    quiet_prices = closes[-3:]
    quiet_net = abs(quiet_prices[-1] / quiet_prices[0] - 1) * 100
    quiet_span = np.ptp(quiet_prices) / quiet_prices[0] * 100
    quiet = quiet_net < d.quiet_pct and (efficiency(quiet_prices) < d.quiet_er or quiet_span < d.quiet_pct)

    def make(direction, kind, source, er, units, evidence):
        return Signal(symbol, direction, kind, source, price, changes, er, units,
                      adx, plus, minus, now_ms, evidence)

    sustained = None
    for count in (2, 3, 4):
        p = closes[-count - 1:]
        delta = p[-1] - p[0]
        sign = 1 if delta > 0 else -1
        direction = "up" if sign > 0 else "down"
        steps = np.diff(p) * sign
        # At least two advancing closes; small pullbacks allowed; latest close still advances.
        if (direction in config.directions and steps[-1] > 0 and (steps > 0).sum() >= 2
                and (price - closes[-1]) * sign >= -d.pullback_atr * atr
                and steps.min() >= -d.pullback_atr * atr and efficiency(p) >= d.er_min
                and abs(delta / p[0]) * 100 >= d.sustained_pct * d.sensitivity
                and abs(delta) / atr >= d.sustained_atr * d.sensitivity):
            sustained = make(direction, "sustained", "closed", efficiency(p), abs(delta) / atr, last)
            break

    fast = None
    # Include current candle displacement even when the longer window is dominated by a reversal.
    current = candles.iloc[-1]
    candidates = [(0, float(current.open), (price / float(current.open) - 1) * 100),
                  (15, bases[15], changes[15]), (30, bases[30], changes[30])]
    for window, base, pct in candidates:
        threshold = d.fast_30_pct if window == 30 else d.fast_15_pct
        direction = "up" if pct > 0 else "down"
        if (direction in config.directions and abs(pct) >= threshold * d.sensitivity
                and abs(price - base) / atr >= d.fast_atr * d.sensitivity):
            start = now_ms - window * 60_000 if window else int(current.time)
            path = minutes.loc[minutes.time >= start, "close"].to_numpy(dtype=float)
            fast = make(direction, "fast", "forming", efficiency(np.r_[base, path]),
                        abs(price - base) / atr, int(current.time))
            break
    # A fast opposite move supersedes a now-obsolete completed-candle direction.
    chosen = fast if fast and sustained and fast.direction != sustained.direction else sustained or fast
    return Observation(chosen, bool(quiet and chosen is None), last)
