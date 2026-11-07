"""REST transport, server clock and validated rolling candle caches."""
import time
from email.utils import parsedate_to_datetime

import httpx
import numpy as np
import pandas as pd
from .network import DoHTransport


class MarketError(RuntimeError):
    pass


class RateLimited(MarketError):
    pass


class Binance:
    def __init__(self, base_url="https://fapi.binance.com", client=None, retries=2, dns_over_https=False):
        if client is not None:
            self.client = client
        elif dns_over_https:
            transport = DoHTransport(httpx.URL(base_url).host)
            self.client = httpx.Client(base_url=base_url, timeout=10, transport=transport, trust_env=False)
        else:
            self.client = httpx.Client(base_url=base_url, timeout=10)
        self.retries = retries
        self.blocked_until = 0.0
        self.clock = None

    def close(self):
        self.client.close()

    def get(self, path, **params):
        if time.monotonic() < self.blocked_until:
            raise RateLimited("Binance global backoff active")
        for attempt in range(self.retries + 1):
            try:
                response = self.client.get(path, params=params)
                if response.status_code in (418, 429):
                    retry = response.headers.get("Retry-After", "60")
                    try:
                        delay = float(retry)
                    except ValueError:
                        try:
                            delay = parsedate_to_datetime(retry).timestamp() - time.time()
                        except (ValueError, TypeError):
                            delay = 60
                    self.blocked_until = time.monotonic() + max(1, delay)
                    raise RateLimited(f"Binance HTTP {response.status_code}; backoff {max(1, delay):.0f}s")
                if response.status_code >= 500:
                    raise MarketError(f"Binance HTTP {response.status_code}")
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and "code" in data:
                    raise MarketError(f"Binance API code {data['code']}")
                return data
            except RateLimited:
                raise
            except (httpx.TransportError, MarketError) as exc:
                if attempt == self.retries:
                    raise MarketError(f"market request failed: {type(exc).__name__}") from None
                time.sleep(0.5 * 2 ** attempt)
            except (httpx.HTTPStatusError, ValueError) as exc:
                raise MarketError(f"market response invalid: {type(exc).__name__}") from None

    def sync_clock(self):
        for _ in range(2):
            start = time.monotonic()
            server = int(self.get("/fapi/v1/time")["serverTime"])
            end = time.monotonic()
            if end - start <= 5:
                self.clock = (server, (start + end) / 2)
                return
            # Cold DNS/TLS setup can dominate the first sample. Resample over
            # the warm connection; do not relax the clock-accuracy requirement.
        raise MarketError("server clock request too slow")

    def now_ms(self):
        if self.clock is None:
            raise MarketError("server clock not initialized")
        return int(self.clock[0] + (time.monotonic() - self.clock[1]) * 1000)

    def active_symbols(self):
        info = self.get("/fapi/v1/exchangeInfo")
        return {s["symbol"] for s in info["symbols"] if s["status"] == "TRADING"}

    def symbol_info(self, symbol):
        """One contract's exchangeInfo entry; works whether or not the endpoint filters by symbol."""
        data = self.get("/fapi/v1/exchangeInfo", symbol=symbol)
        for item in data.get("symbols", []):
            if item.get("symbol") == symbol:
                return item
        raise MarketError(f"标的不存在: {symbol}")

    def derivatives(self, symbol):
        """持仓量变化与资金费率；只用于观测，调用方负责吞掉失败。

        代币成交量相对正股很小（NVDA 约 1.9%、AVGO 0.35%），用它推断"真实参与度"
        并不可靠；OI 与资金费率是该合约本身的真实仓位与拥挤度，不依赖跨市场可比性。
        """
        history = self.get("/futures/data/openInterestHist", symbol=symbol, period="15m", limit=5)
        funding = self.get("/fapi/v1/premiumIndex", symbol=symbol)
        values = [float(item["sumOpenInterestValue"]) for item in history] if isinstance(history, list) else []
        change = None
        if len(values) >= 2 and values[0] > 0 and values[-1] > 0:
            change = (values[-1] / values[0] - 1) * 100
        rate = None
        if isinstance(funding, dict) and "lastFundingRate" in funding:
            rate = float(funding["lastFundingRate"]) * 100
        # 绝对量也要留：只有变化率无法判断这个合约本身的体量。
        latest = values[-1] if values else None
        return {"oi_change": change, "funding_rate": rate, "oi_value": latest}

    def validate_symbols(self, symbols):
        active = self.active_symbols()
        invalid = sorted(set(symbols) - active)
        if invalid:
            raise MarketError(f"标的不存在或不可交易: {', '.join(invalid)}")
        return active

# quote_volume / taker_buy_* 只用于观测（资金规模、主动买卖占比），不参与检测阈值。
COLUMNS = ["time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
           "trades", "taker_buy_volume", "taker_buy_quote_volume"]


def parse_klines(rows):
    try:
        frame = pd.DataFrame([[int(r[0]), *map(float, r[1:6]), int(r[6]), float(r[7]),
                               int(r[8]), float(r[9]), float(r[10])]
                              for r in rows], columns=COLUMNS)
    except (ValueError, TypeError, IndexError) as exc:
        raise MarketError("malformed klines") from exc
    if frame.empty:
        raise MarketError("empty klines")
    # A mutable candle may occur more than once; the last version wins.
    return frame.drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True)


def validate_frame(frame, interval_ms, now_ms):
    if frame.empty or not np.isfinite(frame.to_numpy(dtype=float)).all():
        raise MarketError("empty or nonfinite candles")
    if (np.diff(frame.time) != interval_ms).any():
        raise MarketError("missing or duplicate candles")
    if (frame.time % interval_ms != 0).any() or (frame.close_time != frame.time + interval_ms - 1).any():
        raise MarketError("invalid candle timestamps")
    if ((frame[["open", "high", "low", "close"]] <= 0).any().any()
            or (frame.volume < 0).any() or (frame.trades < 0).any()
            or (frame[["quote_volume", "taker_buy_volume", "taker_buy_quote_volume"]] < 0).any().any()
            or (frame.high < frame[["open", "close", "low"]].max(axis=1)).any()
            or (frame.low > frame[["open", "close", "high"]].min(axis=1)).any()):
        raise MarketError("invalid OHLCV")
    if int(frame.time.iloc[-1]) != now_ms // interval_ms * interval_ms:
        raise MarketError(
            f"stale/future candle or boundary not yet available: "
            f"latest={int(frame.time.iloc[-1])}, expected={now_ms // interval_ms * interval_ms}"
        )


class CandleCache:
    def __init__(self):
        self.frames = {}

    def refresh(self, api, symbol, interval, interval_ms, history):
        key = (symbol, interval)
        old = self.frames.get(key)
        params = dict(symbol=symbol, interval=interval, limit=history)
        if old is not None:
            needed = (api.now_ms() - int(old.time.iloc[-1])) // interval_ms + 2
            if needed < history:
                params.update(startTime=int(old.time.iloc[-2]), limit=max(3, int(needed)))
        try:
            incoming = parse_klines(api.get("/fapi/v1/klines", **params))
            frame = incoming if old is None or "startTime" not in params else pd.concat([old, incoming])
            frame = frame.drop_duplicates("time", keep="last").sort_values("time").tail(history).reset_index(drop=True)
            validate_frame(frame, interval_ms, api.now_ms())
            if len(frame) < history - 1:
                raise MarketError("insufficient history (possibly newly listed symbol)")
        except Exception:
            self.frames.pop(key, None)  # next scan refills; never substitute stale data
            raise
        self.frames[key] = frame
        return frame
