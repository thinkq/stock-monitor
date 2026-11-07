import argparse
import logging
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config, load
from .detection import detect
from .market import Binance, CandleCache, MarketError, RateLimited
from .notify import Notifier
from .state import State, day_of

LOG = logging.getLogger(__name__)


def _number(value):
    """只接受真实数值；Mock 或异常类型一律视为缺失，避免污染 payload 与 JSON 序列化。"""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def minute_bars(config):
    """拉多少根 1 分钟 K 线：至少 65 根（覆盖 60 分钟位移 + 1 根确认）。"""
    return max(65, config.history_bars) if config.interval == "1m" else 65


def breadth_of(symbol, direction, seen, total):
    """同向信号的标的数（含自身）：本轮已扫 + 上一轮快照。

    纯函数、无 I/O，便于测试。信号不再成立时会从 seen 中移除，因此它反映的是
    "当前仍在报警的标的"，不是"本轮价格同向的标的"。
    """
    peers = {name for name, (side, _kind) in seen.items() if side == direction and name != symbol}
    return min(len(peers) + 1, max(int(total), 1))


class Monitor:
    def __init__(self, config, api, state, notifier):
        self.config, self.api, self.state, self.notifier = config, api, state, notifier
        self.cache = CandleCache()
        self.last_errors = {}
        # 跨标的共振快照：本轮逐个扫，未扫到的标的用上一轮的值（最多滞后一个扫描周期）。
        self.round_signals = {}
        self.prev_signals = {}
        # 已预留、等待投递的告警：一轮结束后统一发，才能判断要不要合并成板块消息。
        self.pending = []

    def tag_derivatives(self, symbol, obs):
        """补上持仓量与资金费率。只做观测：拉取失败不改变扫描结果，也不阻塞报警。"""
        if obs.signal is None:
            return obs
        try:
            extra = self.api.derivatives(symbol)
        except RateLimited:
            raise
        except (MarketError, KeyError, TypeError, ValueError) as exc:
            LOG.warning("%s derivatives unavailable: %s", symbol, exc)
            return obs
        if not isinstance(extra, dict):          # 单测里 api 可能是 Mock
            return obs
        return replace(obs, signal=replace(obs.signal, oi_change=_number(extra.get("oi_change")),
                                           funding_rate=_number(extra.get("funding_rate")),
                                           oi_value=_number(extra.get("oi_value"))))

    def tag_breadth(self, symbol, obs):
        """给信号补上同向广度；只记录，不参与是否报警的判定。"""
        signal = obs.signal
        if signal is None:
            self.round_signals.pop(symbol, None)
            return obs
        seen = {**self.prev_signals, **self.round_signals}
        total = len(self.config.symbols)
        tagged = replace(signal, breadth=breadth_of(signal.symbol, signal.direction, seen, total),
                         breadth_total=total)
        self.round_signals[signal.symbol] = (signal.direction, signal.kind)
        return replace(obs, signal=tagged)

    def scan_symbol(self, symbol):
        cfg, api = self.config, self.api
        candles = self.cache.refresh(api, symbol, cfg.interval, cfg.interval_ms, cfg.history_bars)
        minutes = self.cache.refresh(api, symbol, "1m", 60_000, minute_bars(cfg))
        # Last-trade time provides a freshness check in addition to candle timestamps.
        ticker = api.get("/fapi/v2/ticker/price", symbol=symbol)
        now = api.now_ms()
        age = now - int(ticker["time"])
        if ticker["symbol"] != symbol or not -5000 <= age <= cfg.max_data_age_seconds * 1000:
            raise MarketError("stale or mismatched last-trade timestamp")
        obs = self.tag_breadth(symbol, self.tag_derivatives(
            symbol, detect(symbol, candles, minutes, now, cfg)))
        reserved = self.state.observe(symbol, obs, now, cfg)
        if reserved is not None:              # 升级提醒会带回补了 escalation_from 的信号
            self.pending.append((reserved.alert_id, reserved.signal))
        return obs

    def check_health(self, good):
        """扫描不完整时的心跳：连续 N 轮缺标的就说一句，避免"以为在盯着"。

        只处理"部分成功"（退出码仍是 0，cron 不会报错）。整轮失败由退出码 1 通知。
        """
        total = len(self.config.symbols)
        meta = self.state.get_meta("health") or {}
        now = self.api.now_ms()
        if good >= total:
            if meta.get("streak", 0) >= max(self.config.health_alert_rounds, 1) and meta.get("notified_at"):
                self.notifier.send_notice(f"✅ **监控已恢复：{total}/{total} 标的扫描成功**")
            self.state.set_meta("health", {"streak": 0, "notified_at": None, "last_good": now})
            return
        streak = int(meta.get("streak", 0)) + 1
        notified_at = meta.get("notified_at")
        due = (self.config.health_alert_rounds > 0 and streak >= self.config.health_alert_rounds
               and (notified_at is None or now - notified_at >= self.config.health_alert_seconds * 1000))
        if due:
            missing = sorted(self.last_errors)[:4]
            detail = "、".join(missing) + ("等" if len(self.last_errors) > 4 else "")
            self.notifier.send_notice(
                f"⚠️ **监控不完整（连续 {streak} 轮）**\n\n"
                f"最近一轮：{good}/{total} 成功｜{datetime.fromtimestamp(now / 1000, ZoneInfo(self.config.timezone)):%m-%d %H:%M}\n"
                f"缺失：{detail}\n"
                f"影响：这些标的这一轮不判异动，异动可能漏报。")
            notified_at = now
        self.state.set_meta("health", {"streak": streak, "notified_at": notified_at,
                                       "last_good": meta.get("last_good")})

    def prune_once(self):
        """每轮开头尝试一次（内部按天去重），老告警不会无限堆积。"""
        today = day_of(self.api.now_ms(), self.config.timezone)
        removed = self.state.maybe_prune(today, self.config.retention_days)
        if removed:
            LOG.info("pruned %s alerts older than %s days", removed, self.config.retention_days)

    def _flush(self):
        """一轮结束统一投递：同向达到阈值就合并成一条板块消息，不再逐条刷屏。"""
        pending, self.pending = self.pending, []
        if not pending:
            return
        groups = {}
        for alert_id, signal in pending:
            groups.setdefault(signal.direction, []).append((alert_id, signal))
        minimum = self.config.merge_min_symbols
        for items in groups.values():
            if minimum >= 2 and len(items) >= minimum:
                self._deliver_batch(items)
            else:
                for alert_id, signal in items:
                    self._deliver(alert_id, signal)

    def _deliver(self, alert_id, signal):
        try:
            status = self.notifier.send(signal)
        except RuntimeError:
            self.state.finish(alert_id, "failed", "delivery failed or uncertain; no automatic resend")
            LOG.error("%s notification delivery failed/uncertain; see SQLite alert %s",
                      signal.symbol, alert_id)
        else:
            self.state.finish(alert_id, status)

    def _deliver_batch(self, items):
        try:
            status = self.notifier.send_batch([signal for _alert, signal in items])
        except RuntimeError:
            for alert_id, signal in items:
                self.state.finish(alert_id, "failed", "batch delivery failed or uncertain; no resend")
                LOG.error("%s batch delivery failed/uncertain; see SQLite alert %s", signal.symbol, alert_id)
        else:
            for alert_id, _signal in items:
                self.state.finish(alert_id, status, f"batched:{len(items)}")

    def scan(self):
        # Any clock failure aborts the whole scan without touching episode state.
        try:
            self.api.sync_clock()
        except (MarketError, KeyError, TypeError, ValueError):
            self.state.clear_pending()
            raise
        good = 0
        self.last_errors = {}
        self.round_signals = {}
        self.pending = []
        self.prune_once()
        try:
            for symbol in self.config.symbols:
                try:
                    obs = self.scan_symbol(symbol)
                    good += 1
                    LOG.info("%s fresh; signal=%s", symbol, obs.signal.kind if obs.signal else "none")
                except RateLimited:
                    self.state.clear_pending()
                    raise  # one global backoff, not 20 independent retry storms
                except (MarketError, KeyError, TypeError, ValueError) as exc:
                    self.last_errors[symbol] = str(exc)
                    self.state.clear_pending(symbol)
                    LOG.warning("%s skipped: %s", symbol, exc)
        finally:
            # 即使本轮中途被限流中止，已经预留的告警也必须发出去，不能烂在 reserved 里。
            self.prev_signals = dict(self.round_signals)
            self._flush()
        self.check_health(good)
        return good


def verify(config, api, output):
    api.sync_clock()
    api.validate_symbols(config.symbols)
    cache = CandleCache()
    Path(output).mkdir(parents=True, exist_ok=True)
    for symbol in config.symbols:
        frame = cache.refresh(api, symbol, config.interval, config.interval_ms, config.history_bars)
        minutes = cache.refresh(api, symbol, "1m", 60_000, minute_bars(config))
        ticker = api.get("/fapi/v2/ticker/price", symbol=symbol)
        if not -5000 <= api.now_ms() - int(ticker["time"]) <= config.max_data_age_seconds * 1000:
            raise MarketError("stale ticker during verification")
        obs = detect(symbol, frame, minutes, api.now_ms(), config)
        frame.to_csv(Path(output) / f"{symbol}-{config.interval}.csv", index=False)
        minutes.to_csv(Path(output) / f"{symbol}-1m.csv", index=False)
        print(f"VERIFIED {symbol}: {len(frame)} candles; indicators ready; signal={obs.signal.kind if obs.signal else 'none'}")


def resolve_paths(config: "Config", config_path: Path):
    """Anchor relative database/output paths at the config file, not the caller's cwd."""
    database = Path(config.database).expanduser()
    if not database.is_absolute():
        database = config_path.parent / database
    return replace(config, database=str(database.resolve()))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"status", "config", "help", "list", "ls", "add", "del",
                            "delete", "rm", "check", "reset", "audit"}:
        from .admin import main as admin_main
        return admin_main(argv)
    parser = argparse.ArgumentParser(description="Binance Futures 行情异动监控（无交易功能）")
    parser.add_argument("command", choices=["run", "once", "verify", "demo"], nargs="?", default="run")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--output", default="data/verification")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # Telegram URLs contain Bot tokens.
    api = state = notifier = None
    try:
        config_path = Path(args.config).expanduser().resolve()
        config = resolve_paths(load(config_path), config_path)
        if args.command == "demo":
            from .demo import run_demo
            run_demo(config)
            return 0
        output = Path(args.output).expanduser()
        if not output.is_absolute():
            output = config_path.parent / output
        api = Binance(config.base_url, dns_over_https=config.dns_over_https)
        if args.command == "verify":
            verify(config, api, output)
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
