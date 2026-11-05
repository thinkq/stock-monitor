import argparse
import logging
import time
from pathlib import Path

from .config import load
from .detection import detect
from .market import Binance, CandleCache, MarketError, RateLimited
from .notify import Notifier
from .state import State

LOG = logging.getLogger(__name__)


class Monitor:
    def __init__(self, config, api, state, notifier):
        self.config, self.api, self.state, self.notifier = config, api, state, notifier
        self.cache = CandleCache()

    def scan_symbol(self, symbol):
        cfg, api = self.config, self.api
        candles = self.cache.refresh(api, symbol, cfg.interval, cfg.interval_ms, cfg.history_bars)
        minutes = self.cache.refresh(api, symbol, "1m", 60_000, max(65, cfg.history_bars) if cfg.interval == "1m" else 65)
        # Last-trade time provides a freshness check in addition to candle timestamps.
        ticker = api.get("/fapi/v2/ticker/price", symbol=symbol)
        now = api.now_ms()
        age = now - int(ticker["time"])
        if ticker["symbol"] != symbol or not -5000 <= age <= cfg.max_data_age_seconds * 1000:
            raise MarketError("stale or mismatched last-trade timestamp")
        obs = detect(symbol, candles, minutes, now, cfg)
        alert_id = self.state.observe(symbol, obs, now, cfg)
        if alert_id is not None:
            try:
                status = self.notifier.send(obs.signal)
            except RuntimeError:
                self.state.finish(alert_id, "failed", "delivery failed or uncertain; no automatic resend")
                LOG.error("%s notification delivery failed/uncertain; see SQLite alert %s", symbol, alert_id)
            else:
                self.state.finish(alert_id, status)
        return obs

    def scan(self):
        # Any clock failure aborts the whole scan without touching episode state.
        self.api.sync_clock()
        good = 0
        for symbol in self.config.symbols:
            try:
                obs = self.scan_symbol(symbol)
                good += 1
                LOG.info("%s fresh; signal=%s", symbol, obs.signal.kind if obs.signal else "none")
            except RateLimited:
                raise  # one global backoff, not 20 independent retry storms
            except (MarketError, KeyError, TypeError, ValueError) as exc:
                LOG.warning("%s skipped: %s", symbol, exc)
        return good


def verify(config, api, output):
    api.sync_clock()
    api.validate_symbols(config.symbols)
    cache = CandleCache()
    Path(output).mkdir(parents=True, exist_ok=True)
    for symbol in config.symbols:
        frame = cache.refresh(api, symbol, config.interval, config.interval_ms, config.history_bars)
        minutes = cache.refresh(api, symbol, "1m", 60_000, max(65, config.history_bars) if config.interval == "1m" else 65)
        ticker = api.get("/fapi/v2/ticker/price", symbol=symbol)
        if not -5000 <= api.now_ms() - int(ticker["time"]) <= config.max_data_age_seconds * 1000:
            raise MarketError("stale ticker during verification")
        obs = detect(symbol, frame, minutes, api.now_ms(), config)
        frame.to_csv(Path(output) / f"{symbol}-{config.interval}.csv", index=False)
        minutes.to_csv(Path(output) / f"{symbol}-1m.csv", index=False)
        print(f"VERIFIED {symbol}: {len(frame)} candles; indicators ready; signal={obs.signal.kind if obs.signal else 'none'}")


def main():
    parser = argparse.ArgumentParser(description="Binance Futures 行情异动监控（无交易功能）")
    parser.add_argument("command", choices=["run", "once", "verify", "demo"], nargs="?", default="run")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--output", default="data/verification")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # Telegram URLs contain Bot tokens.
    api = state = notifier = None
    try:
        config = load(args.config)
        if args.command == "demo":
            from .demo import run_demo
            run_demo(config)
            return 0
        api = Binance(config.base_url, dns_over_https=config.dns_over_https)
        if args.command == "verify":
            verify(config, api, args.output)
            return 0
        # Fail clearly before running if a requested symbol is not trading.
        api.validate_symbols(config.symbols)
        state = State(config.database)
        notifier = Notifier(config)
        monitor = Monitor(config, api, state, notifier)
        while True:
            start = time.monotonic()
            try:
                good = monitor.scan()
            except (MarketError, KeyError, ValueError, TypeError) as exc:
                LOG.error("scan skipped: %s", exc)
                good = 0
            if args.command == "once":
                return 0 if good == len(config.symbols) else 1
            delay = max(0, config.scan_seconds - (time.monotonic() - start), api.blocked_until - time.monotonic())
            # Interruptible sleep; the CLI never catches Ctrl-C as a network failure.
            time.sleep(delay)
    except KeyboardInterrupt:
        LOG.info("stopped")
        return 0
    except (MarketError, ValueError, OSError, KeyError, TypeError) as exc:
        LOG.error("startup/verification failed: %s", exc)
        return 1
    finally:
        for resource in (notifier, state, api):
            if resource is not None:
                resource.close()


if __name__ == "__main__":
    raise SystemExit(main())
