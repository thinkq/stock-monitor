"""Deterministic synthetic fixtures, explicitly not real market data."""
from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd

from .config import Config
from .detection import detect
from .market import COLUMNS
from .notify import format_message
from .state import State


def scenario(tail=(101.0, 102.1, 103.2), forming=None, interval_ms=900_000):
    history = [100 + (0.025 if i % 2 else -0.025) for i in range(100)]
    prices = history + list(tail)
    current = prices[-1] if forming is None else forming
    start = int(datetime(2026, 11, 4, tzinfo=timezone.utc).timestamp() * 1000)
    start = start // interval_ms * interval_ms
    rows = []
    for i, price in enumerate(prices + [current]):
        opening = prices[i - 1] if i else prices[0]
        t = start + i * interval_ms
        rows.append([t, opening, max(opening, price) + 0.08,
                     min(opening, price) - 0.08, price, 100., t + interval_ms - 1, 100])
    candles = pd.DataFrame(rows, columns=COLUMNS)
    # Observation halfway through the mutable candle, at an exact minute boundary + 30 seconds.
    current_start = int(candles.time.iloc[-1])
    now = current_start + max(30_000, interval_ms // 2 // 60_000 * 60_000 + 30_000)
    minute_rows = []
    for t in range(now // 60_000 * 60_000 - 64 * 60_000, now // 60_000 * 60_000 + 1, 60_000):
        index = (t - start) // interval_ms
        bar = candles.iloc[index]
        end = now if index == len(candles) - 1 else int(bar.close_time) + 1
        progress_start = (t - int(bar.time)) / (end - int(bar.time))
        progress_end = (min(t + 60_000, now) - int(bar.time)) / (end - int(bar.time))
        opening = bar.open + (bar.close - bar.open) * progress_start
        closing = bar.open + (bar.close - bar.open) * progress_end
        minute_rows.append([t, opening, max(opening, closing) + .01, min(opening, closing) - .01,
                            closing, 10., t + 59_999, 10])
    return candles, pd.DataFrame(minute_rows, columns=COLUMNS), now


def run_demo(config: Config):
    cfg = replace(config, interval="15m", directions=["up", "down"])
    print("=== 合成数据演示；不是 Binance 实时行情；不会发送 Telegram ===")
    state = State(":memory:")
    try:
        for symbol, tail, forming in [
            ("DEMO_UP", (101, 102.1, 103.2), None),
            ("DEMO_DOWN", (99, 97.9, 96.8), None),
            ("DEMO_FLAT", (100.02, 99.98, 100.01), None),
            ("DEMO_FAST", (100, 100, 100), 102.5),
            ("DEMO_REVERSE", (101, 102.2, 103.5), 98),
        ]:
            bars, minutes, now = scenario(tail, forming)
            obs = detect(symbol, bars, minutes, now, cfg)
            first = state.observe(symbol, obs, now, cfg)
            again = state.observe(symbol, obs, now + 1000, cfg)
            print(f"\n{symbol}: first_alert={first}, repeat_alert={again}")
            if first:
                print(format_message(obs.signal, cfg.timezone).split("https://")[0]
                      .replace("数据来源：Binance Futures", "数据来源：合成数据（非真实行情）"))
                state.finish(first, "console")
    finally:
        state.close()
