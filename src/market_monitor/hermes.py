"""One deterministic scan; Hermes owns scheduling and message delivery."""
import argparse
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import logging
from pathlib import Path
import sys
import time

from .app import Monitor
from .config import Config, load
from .market import Binance, MarketError
from .notify import format_chat_alert, format_sector_alert
from .state import State

LOG = logging.getLogger(__name__)

# exchangeInfo is ~1.2 MB. Rebuilding it every minute costs ~1.7 GB/day for a
# symbol set that changes a few times a month, so the trading set is cached and
# dropped whenever a scan comes back incomplete (a delisted or halted symbol then
# gets re-validated on the next run instead of failing silently forever).
SYMBOL_CACHE_SECONDS = 24 * 3600


@contextmanager
def scan_lock(database):
    """Do not let overlapping cron fires interleave confirmation observations."""
    path = Path(str(database) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class HermesNotifier:
    """Write to the scheduler, without claiming downstream delivery succeeded."""

    def __init__(self, config, stream=None):
        self.config = config
        self.failed = False
        self.sent = 0
        self.stream = stream if stream is not None else sys.stdout

    def send(self, signal):
        return self.send_batch([signal])

    def send_notice(self, text):
        """系统级提示：与告警共用 stdout，cron 会一起投递。"""
        return self._write(text, 1)

    def send_batch(self, signals):
        """一轮的多条告警：同向达到阈值时输出一条板块级消息，否则逐块拼装。"""
        minimum = self.config.merge_min_symbols
        if minimum >= 2 and len(signals) >= minimum:
            text = format_sector_alert(signals, self.config.timezone)
        else:
            text = "\n\n---\n\n".join(format_chat_alert(s, self.config.timezone) for s in signals)
        return self._write(text, len(signals))

    def _write(self, text, count):
        """唯一写路径：块间空一行 + 分隔线，与 EMA Watch 的批布局一致。"""
        try:
            prefix = "" if self.sent == 0 else "---\n\n"
            self.stream.write(prefix + text + "\n\n")
            self.stream.flush()
            self.sent += count
        except OSError:
            self.failed = True
            raise RuntimeError("Hermes output failed or uncertain") from None
        return "emitted"


def validate_symbols(api, state: State, config: Config):
    """Reuse the cached trading set; refresh at most once per SYMBOL_CACHE_SECONDS."""
    cached = state.get_symbol_cache()
    if cached is not None:
        fetched_at, active = cached
        if time.time() - fetched_at < SYMBOL_CACHE_SECONDS and set(config.symbols) <= active:
            return
    try:
        active = api.active_symbols()
    except MarketError:
        state.clear_symbol_cache()
        raise
    state.set_symbol_cache(active, time.time())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hermes 单次监测：仅新信号输出到 stdout")
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        config_path = Path(args.config).expanduser().resolve()
        config = load(config_path)
        # Cron's working directory must not create a second deduplication database.
        database = Path(config.database).expanduser()
        if not database.is_absolute():
            database = config_path.parent / database
        config = replace(config, database=str(database.resolve()))
        with scan_lock(config.database) as acquired:
            if not acquired:
                # Distinguishable from "nothing was moving": logs only.
                LOG.warning("previous scan still running; tick skipped")
                return 0
            state = State(config.database)
            try:
                api = Binance(config.base_url, dns_over_https=config.dns_over_https)
                notifier = HermesNotifier(config)
                monitor = Monitor(config, api, state, notifier)
                started = time.monotonic()
                try:
                    validate_symbols(api, state, config)
                    good = monitor.scan()
                except (MarketError, KeyError, ValueError, TypeError):
                    state.clear_pending()
                    raise
                finally:
                    api.close()
                errors = getattr(monitor, "last_errors", None)
                if not isinstance(errors, dict):
                    errors = {}
                now = time.time()
                previous = state.get_meta("last_scan") or {}
                state.set_meta("last_scan", {
                    "ts": now,
                    # 记录实际间隔：cron 的 "every 1m" 实测是 ~120 秒，写入后 mm 能直接看出来。
                    "gap_s": round(now - float(previous["ts"]), 1) if previous.get("ts") else None,
                    "good": good,
                    "total": len(config.symbols),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "errors": {str(key): str(value) for key, value in errors.items()},
                })
                # A partial scan must never cost the alerts already on stdout:
                # Hermes discards stdout when the exit code is non-zero.
                if good != len(config.symbols):
                    state.clear_symbol_cache()
                if notifier.failed:
                    LOG.error("alert output failed or uncertain")
                    return 1
                if good == 0:
                    LOG.error("no symbol scanned successfully")
                    return 1
                if good != len(config.symbols):
                    LOG.warning("partial scan: %s/%s symbols; stdout still delivered",
                                good, len(config.symbols))
                return 0
            finally:
                state.close()
    except (MarketError, ValueError, OSError, KeyError, TypeError) as exc:
        LOG.error("Hermes scan failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
