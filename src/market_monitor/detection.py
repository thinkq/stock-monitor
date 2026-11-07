"""Pure detection: no I/O, clocks, state, or future candles."""
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

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
    # 以下字段只记录、不参与筛选（见 README「待观察字段」）。默认值让旧调用方继续可用。
    window: int = 0                      # sustained 实际命中的 K 线根数 2/3/4（fast 为 0）
    vol_ratio: float | None = None       # 本会话内量能比；样本不足时为 None，绝不填 0
    atr_expansion: float | None = None   # 当前 ATR ÷ 近 50 根 ATR 中位；>1 表示波动率扩张
    session: str = ""                    # 美东会话：RTH / pre / post / night / weekend
    taker_buy_ratio: float | None = None # 窗口内主动买占比（0-1），同场订单流
    oi_change: float | None = None       # 名义持仓量近 1 小时变化%（由 app 层补）
    funding_rate: float | None = None    # 资金费率%（由 app 层补）
    breadth: int = 0                     # 同向信号的标的数（本轮 + 上轮快照，含自身）
    breadth_total: int = 0               # 参与统计的标的数


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


NEW_YORK = ZoneInfo("America/New_York")


def session_of(timestamp_ms: int) -> str:
    """美东会话标签。周末单列，因为美股休市时的量级不具可比性（节假日未单独处理）。"""
    moment = datetime.fromtimestamp(int(timestamp_ms) / 1000, NEW_YORK)
    if moment.weekday() >= 5:
        return "weekend"
    minute = moment.hour * 60 + moment.minute
    if 9 * 60 + 30 <= minute < 16 * 60:
        return "RTH"
    if 4 * 60 <= minute < 9 * 60 + 30:
        return "pre"
    if 16 * 60 <= minute < 20 * 60:
        return "post"
    return "night"


def volume_ratio(closed: pd.DataFrame, bars: int, sessions=None,
                 lookback: int = 120, min_samples: int = 8):
    """同会话量能比：最近 bars 根均量 ÷ 同会话历史中位量。

    跨会话比较没有意义（正式盘的量级与盘前/夜盘不同），因此基准只取同一会话的历史。
    同会话样本不足时返回 None——宁可显示"—"也不输出一个会误导人的数字。
    """
    volume = closed.volume.to_numpy(dtype=float)
    bars = max(1, int(bars))
    if len(volume) < bars + 1:
        return None
    labels = sessions if sessions is not None else ["RTH"] * len(volume)
    current = labels[-1]
    start = max(0, len(volume) - bars - lookback)
    base = [volume[j] for j in range(start, len(volume) - bars) if labels[j] == current]
    if len(base) < min_samples:
        return None
    reference = float(np.median(base))
    if reference <= 0:
        return None
    return float(volume[-bars:].mean() / reference)


def taker_buy_ratio(closed: pd.DataFrame, bars: int):
    """窗口内主动买占比（同场订单流，尺度无关）。缺列或零量时返回 None。"""
    if "taker_buy_volume" not in closed:
        return None
    bars = max(1, int(bars))
    taken = float(closed.taker_buy_volume.to_numpy(dtype=float)[-bars:].sum())
    total = float(closed.volume.to_numpy(dtype=float)[-bars:].sum())
    return float(taken / total) if total > 0 else None


def expansion_ratio(closed: pd.DataFrame, config: Config):
    """当前 ATR ÷ 近 50 根 ATR 中位。与检测同一口径取 [-5]，排除最近 4 根避免自我抬高。"""
    d = config.detection
    h, l, c = (closed[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    series = talib.ATR(h, l, c, timeperiod=d.atr_period)
    if len(series) < d.atr_period + 10:
        return None
    now = series[-5]
    base = series[-55:-5]
    base = base[np.isfinite(base)]
    if not np.isfinite(now) or base.size == 0:
        return None
    middle = float(np.median(base))
    return float(now / middle) if middle > 0 else None


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

    expansion = expansion_ratio(closed, config)
    sessions = [session_of(int(stamp)) for stamp in closed.time]
    current_session = sessions[-1]
    taker = taker_buy_ratio(closed, 4)

    def make(direction, kind, source, er, units, evidence, window=0, vol=None):
        return Signal(symbol, direction, kind, source, price, changes, er, units,
                      adx, plus, minus, now_ms, evidence, window, vol, expansion,
                      current_session, taker)

    sustained = None
    for count in (2, 3, 4):
        p = closes[-count - 1:]
        delta = p[-1] - p[0]
        sign = 1 if delta > 0 else -1
        direction = "up" if sign > 0 else "down"
        steps = np.diff(p) * sign
        # At least two advancing closes; small pullbacks allowed; latest close still advances.
        # 不再校验效率 ER：count=2 时两步必须同向，ER 恒为 1.0，该门槛在 170 个
        # 历史候选上从未拦到任何一条（详见 README「为什么停用 er_min」）。ER 仅作展示。
        if (direction in config.directions and steps[-1] > 0 and (steps > 0).sum() >= 2
                and (price - closes[-1]) * sign >= -d.pullback_atr * atr
                and steps.min() >= -d.pullback_atr * atr
                and abs(delta / p[0]) * 100 >= d.sustained_pct * d.sensitivity
                and abs(delta) / atr >= d.sustained_atr * d.sensitivity):
            # 量能固定用 60 分钟（4 根）累计：单根 15m 在薄盘上噪声太大（CV>1.5）
            sustained = make(direction, "sustained", "closed", efficiency(p), abs(delta) / atr, last,
                             window=count, vol=volume_ratio(closed, 4, sessions))
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
            er = efficiency(np.r_[base, path])
            if er < d.fast_er_min:
                continue
            fast = make(direction, "fast", "forming", er,
                        abs(price - base) / atr, int(current.time), vol=volume_ratio(closed, 4, sessions))
            break
    # A fast opposite move supersedes a now-obsolete completed-candle direction.
    chosen = fast if fast and sustained and fast.direction != sustained.direction else sustained or fast
    return Observation(chosen, bool(quiet and chosen is None), last)
