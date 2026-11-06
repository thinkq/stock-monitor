"""Non-scanning commands: status, config edits (with audit) and help."""
import json
from datetime import datetime
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

import numpy as np
import talib

from .config import Config, load, load_overrides, local_path, save_overrides
from .detection import efficiency
from .market import Binance, MarketError
from .state import State

DEFAULT_CONFIG = "config.toml"
SYMBOL_RE = re.compile(r"[A-Z0-9_]{3,30}")


def normalize_symbol(raw: str) -> str:
    symbol = str(raw).strip().upper().replace("/", "").replace("-", "")
    return symbol if not symbol or symbol.endswith("USDT") else f"{symbol}USDT"


def _db_path(config_path: Path, config: Config) -> Path:
    database = Path(config.database).expanduser()
    return database if database.is_absolute() else config_path.parent / database


def _flatten(overrides: dict) -> list[str]:
    keys = []
    for key, value in overrides.items():
        if isinstance(value, dict):
            keys += [f"{key}.{inner}" for inner in value]
        else:
            keys.append(key)
    return keys


def _direction_text(directions) -> str:
    if set(directions) == {"up", "down"}:
        return "涨+跌"
    return "只涨" if "up" in directions else "只跌"


def _describe_change(key: str, old: str, new: str) -> str:
    if key == "symbols":
        try:
            before, after = set(json.loads(old)), set(json.loads(new))
        except (ValueError, TypeError):
            return f"{old} → {new}"
        added, removed = sorted(after - before), sorted(before - after)
        parts = []
        if added:
            parts.append("加入 " + ", ".join(added))
        if removed:
            parts.append("移除 " + ", ".join(removed))
        return "；".join(parts) or "无变化"
    if key == "reset":
        return "撤销本地覆盖"
    return f"{old} → {new}"


def _audit(config_path: Path, config: Config, key, old, new, source="cli") -> None:
    state = State(_db_path(config_path, config))
    try:
        state.log_config_change(key, old, new, source)
    finally:
        state.close()


def _print(lines: list[str]) -> int:
    print("\n".join(lines))
    return 0


def cmd_status(config_path: Path) -> int:
    config = load(config_path)
    overrides = load_overrides(config_path)
    tz = ZoneInfo(config.timezone)
    lines = [
        "📊 行情异动监控",
        "",
        f"标的：{len(config.symbols)} 只　方向：{_direction_text(config.directions)}",
        f"周期：{config.interval}　扫描：每 {config.scan_seconds} 秒",
        f"灵敏度：{config.detection.sensitivity:g}　冷却：{config.cooldown_seconds // 60} 分钟"
        f"　每日上限：{config.daily_limit}",
        f"配置：{config_path.name}"
        + (f" + {local_path(config_path).name}（{len(_flatten(overrides))} 项覆盖）" if overrides else "（无本地覆盖）"),
    ]
    database = _db_path(config_path, config)
    if not database.exists():
        lines += ["", "状态库：尚未扫描过，等下一条 cron 触发。"]
        return _print(lines)
    state = State(database)
    try:
        scan = state.get_meta("last_scan") or {}
        if scan:
            stamp = datetime.fromtimestamp(float(scan.get("ts") or 0), tz).strftime("%m-%d %H:%M:%S")
            lines += [
                "",
                f"最近一轮：{stamp}　{scan.get('good')}/{scan.get('total')} 成功"
                f"　耗时 {float(scan.get('duration_ms') or 0) / 1000:.1f} 秒",
            ]
            errors = scan.get("errors") or {}
            if errors:
                lines.append("本轮跳过：" + "；".join(f"{k}（{v}）" for k, v in errors.items()))
        day = datetime.now(tz).date().isoformat()
        rows = state.db.execute(
            "SELECT status, count(*) FROM alerts WHERE day=? GROUP BY status", (day,)).fetchall()
        counts = {row[0]: row[1] for row in rows}
        sent = counts.get("sent", 0) + counts.get("emitted", 0)
        lines.append(
            f"今日：告警 {sum(counts.values())} 条（推送 {sent} / 抑制 {counts.get('suppressed', 0)}"
            f" / 失败 {counts.get('failed', 0)}）"
            f"　活跃 episode {state.db.execute('SELECT count(*) FROM episodes WHERE active=1').fetchone()[0]}")
        recent = state.db.execute(
            "SELECT symbol,direction,kind,detected_ms,status FROM alerts ORDER BY id DESC LIMIT 5").fetchall()
        if recent:
            lines += ["", "最近告警："]
            for row in recent:
                stamp = datetime.fromtimestamp(row["detected_ms"] / 1000, tz).strftime("%m-%d %H:%M")
                arrow = "上涨" if row["direction"] == "up" else "下跌"
                lines.append(f"- {stamp} {row['symbol']} {arrow} {row['kind']}（{row['status']}）")
        changes = state.recent_config_changes(3)
        if changes:
            lines += ["", "最近配置改动："]
            for change in changes:
                detail = _describe_change(change["key"], change["old_value"], change["new_value"])
                lines.append(f"- {change['ts']} {change['key']}：{detail}")
    finally:
        state.close()
    return _print(lines)


def cmd_config_list(config_path: Path) -> int:
    config = load(config_path)
    overrides = load_overrides(config_path)
    detection_overrides = overrides.get("detection", {})
    lines = ["📋 生效配置", f"来源：{config_path}"
             + (f" + {local_path(config_path)}" if overrides else "（仅基础文件）"), ""]
    marked = lambda key: " ← 覆盖" if key in overrides else ""
    lines += [
        f"symbols = {config.symbols}{marked('symbols')}",
        f"interval = {config.interval}{marked('interval')}",
        f"scan_seconds = {config.scan_seconds}{marked('scan_seconds')}",
        f"directions = {config.directions}{marked('directions')}",
        f"cooldown_seconds = {config.cooldown_seconds}{marked('cooldown_seconds')}",
        f"daily_limit = {config.daily_limit}{marked('daily_limit')}",
        f"history_bars = {config.history_bars}{marked('history_bars')}",
        f"max_data_age_seconds = {config.max_data_age_seconds}{marked('max_data_age_seconds')}",
        f"timezone = {config.timezone}{marked('timezone')}",
        "",
        "[detection]",
    ]
    for name, value in vars(config.detection).items():
        flag = " ← 覆盖" if name in detection_overrides else ""
        lines.append(f"  {name} = {value}{flag}")
    if overrides and "symbols" not in overrides:
        lines += ["", "（标的未覆盖，改过的是其它项）"]
    return _print(lines)


def _fetch_bars(api: Binance, symbol: str, config: Config) -> list:
    bars = api.get("/fapi/v1/klines", symbol=symbol, interval=config.interval,
                   limit=config.history_bars)
    if not isinstance(bars, list) or not bars:
        raise MarketError("empty klines")
    return bars


def _bars_stats(bars: list, config: Config) -> dict:
    d = config.detection
    high = np.array([float(bar[2]) for bar in bars])
    low = np.array([float(bar[3]) for bar in bars])
    close = np.array([float(bar[4]) for bar in bars])
    open_ = np.array([float(bar[1]) for bar in bars])
    quote_volume = np.array([float(bar[7]) for bar in bars])
    atr = talib.ATR(high, low, close, timeperiod=d.atr_period)[-1]
    moves = np.abs(close[:-1] / open_[:-1] - 1) * 100
    fast_hits = int((moves >= d.fast_15_pct * d.sensitivity).sum())
    flags = np.zeros(len(close), dtype=bool)
    for i in range(4, len(close)):
        window = close[i - 3:i + 1]
        for count in (2, 3, 4):
            path = window[-(count + 1):]
            delta = path[-1] - path[0]
            if delta == 0:
                continue
            sign = 1.0 if delta > 0 else -1.0
            steps = np.diff(path) * sign
            if (steps[-1] > 0 and (steps > 0).sum() >= 2
                    and efficiency(path) >= d.er_min
                    and abs(delta / path[0]) * 100 >= d.sustained_pct * d.sensitivity):
                flags[i] = True
                break
    starts = int((flags[1:-1] & ~flags[:-2]).sum())
    hours = max(1e-9, (len(close) - 1) * config.interval_ms / 3_600_000)
    return {
        "bars": len(close),
        "hours": hours,
        "quote_median": float(np.median(quote_volume)),
        "atr": float(atr) if np.isfinite(atr) else float("nan"),
        "atr_pct": float(atr / close[-1] * 100) if np.isfinite(atr) else float("nan"),
        "fast_hits": fast_hits,
        "sustained_starts": starts,
        "last_price": float(close[-1]),
    }


def cmd_config_check(config_path: Path, raw_symbols: list[str]) -> int:
    config = load(config_path)
    status = 0
    api = Binance(config.base_url, dns_over_https=config.dns_over_https)
    try:
        for raw in raw_symbols:
            symbol = normalize_symbol(raw)
            lines = []
            if not SYMBOL_RE.fullmatch(symbol):
                print(f"❌ {raw}：不是合法标的")
                status = 1
                continue
            try:
                info = api.symbol_info(symbol)
                bars = _fetch_bars(api, symbol, config)
                stats = _bars_stats(bars, config)
            except (MarketError, KeyError, TypeError, ValueError) as exc:
                print(f"❌ {symbol}：{exc}")
                status = 1
                continue
            in_watch = "已在监控" if symbol in config.symbols else "未在监控"
            lines.append(f"🔍 {symbol}（{in_watch}）")
            lines.append(f"状态：{info.get('status')}　现价：{stats['last_price']:.10g}")
            lines.append(f"{config.interval} 成交额中位：${stats['quote_median']:,.0f}"
                         f"　ATR{config.detection.atr_period}：{stats['atr']:.4g}"
                         f"（{stats['atr_pct']:.2f}%）")
            lines.append(
                f"近 {stats['bars']} 根 {config.interval}（约 {stats['hours']:.0f} 小时）："
                f"单根 ≥{config.detection.fast_15_pct:g}% 有 {stats['fast_hits']} 根，"
                f"持续单边启动 {stats['sustained_starts']} 次")
            per_day = stats["sustained_starts"] / stats["hours"] * 24
            lines.append(f"粗估：约 {per_day:.1f} 次/天（仅 {config.interval} 规则，"
                         f"不含快速信号、冷却和每日上限）")
            if stats["quote_median"] < 50_000:
                lines.append("⚠️ 成交额偏低，容易出现几笔单造成的假异动")
            if len(bars) < config.history_bars - 1:
                lines.append(f"⚠️ 历史只有 {len(bars)} 根，指标未就绪，暂时不会报警")
            print("\n".join(lines))
            print()
    finally:
        api.close()
    return status


def cmd_config_add(config_path: Path, raw_symbols: list[str]) -> int:
    config = load(config_path)
    targets = []
    for raw in raw_symbols:
        symbol = normalize_symbol(raw)
        if not SYMBOL_RE.fullmatch(symbol):
            print(f"❌ {raw}：不是合法标的")
            continue
        targets.append(symbol)
    added, rejected = [], []
    api = Binance(config.base_url, dns_over_https=config.dns_over_https)
    try:
        for symbol in targets:
            if symbol in config.symbols:
                rejected.append(f"{symbol}：已在监控")
                continue
            try:
                info = api.symbol_info(symbol)
                if info.get("status") != "TRADING":
                    rejected.append(f"{symbol}：状态 {info.get('status')}")
                    continue
                bars = _fetch_bars(api, symbol, config)
            except (MarketError, KeyError, TypeError, ValueError) as exc:
                rejected.append(f"{symbol}：{exc}")
                continue
            if len(bars) < config.history_bars - 1:
                rejected.append(f"{symbol}：历史只有 {len(bars)} 根 {config.interval}，指标未就绪")
                continue
            added.append(symbol)
    finally:
        api.close()
    lines = []
    if added:
        overrides = load_overrides(config_path)
        overrides["symbols"] = config.symbols + added
        save_overrides(config_path, overrides)
        _audit(config_path, config, "symbols", config.symbols, overrides["symbols"])
        lines.append(f"✅ 已加入：{', '.join(added)}（共 {len(overrides['symbols'])} 只）")
        lines.append("下一轮扫描自动生效，不用重启。")
    if rejected:
        lines.append("❌ 跳过：" + "；".join(rejected))
    if not lines:
        lines.append("没有可加入的标的。")
    return _print(lines) if added else (_print(lines) or 1)


def cmd_config_del(config_path: Path, raw_symbols: list[str]) -> int:
    config = load(config_path)
    targets = [normalize_symbol(raw) for raw in raw_symbols]
    missing = [symbol for symbol in targets if symbol not in config.symbols]
    remaining = [symbol for symbol in config.symbols if symbol not in targets]
    if not remaining:
        print("❌ 不能删空，至少要留一个标的。")
        return 1
    removed = [symbol for symbol in targets if symbol in config.symbols]
    if removed:
        overrides = load_overrides(config_path)
        overrides["symbols"] = remaining
        save_overrides(config_path, overrides)
        _audit(config_path, config, "symbols", config.symbols, remaining)
        state = State(_db_path(config_path, config))
        try:
            for symbol in removed:
                state.db.execute("DELETE FROM episodes WHERE symbol=?", (symbol,))
                state.db.execute("DELETE FROM pending_signals WHERE symbol=?", (symbol,))
        finally:
            state.close()
    lines = []
    if removed:
        lines.append(f"✅ 已移除：{', '.join(removed)}（剩 {len(remaining)} 只，历史告警保留）")
    if missing:
        lines.append("❌ 不在监控里：" + ", ".join(missing))
    return _print(lines) if removed else 1


def cmd_config_reset(config_path: Path, key: str | None) -> int:
    overrides = load_overrides(config_path)
    if not overrides:
        print("没有本地覆盖，已经是 config.toml 的值。")
        return 0
    before = dict(overrides)
    if key is None:
        local_path(config_path).unlink()
        removed = sorted(before)
    else:
        if "." in key:
            table, inner = key.split(".", 1)
            if table not in overrides or inner not in overrides.get(table, {}):
                print(f"❌ {key}：没有这个覆盖项")
                return 1
            del overrides[table][inner]
            if not overrides[table]:
                del overrides[table]
        else:
            if key not in overrides:
                print(f"❌ {key}：没有这个覆盖项")
                return 1
            del overrides[key]
        save_overrides(config_path, overrides)
        removed = [key]
    config = load(config_path)
    _audit(config_path, config, "reset", before, load_overrides(config_path))
    print("✅ 已恢复基础配置：" + ", ".join(removed))
    return 0


def cmd_config_audit(config_path: Path, limit: int = 20) -> int:
    config = load(config_path)
    database = _db_path(config_path, config)
    if not database.exists():
        print("状态库还不存在，没有改动记录。")
        return 0
    state = State(database)
    try:
        rows = state.recent_config_changes(limit)
    finally:
        state.close()
    if not rows:
        print("没有配置改动记录。")
        return 0
    lines = [f"🧾 最近 {len(rows)} 条配置改动"]
    lines += [f"- {row['ts']} {row['key']}：{_describe_change(row['key'], row['old_value'], row['new_value'])}"
              for row in rows]
    return _print(lines)


HELP = """📖 mm 命令（任何目录都能用）

mm — 状态总览
mm help — 这份说明
mm list — 生效配置和覆盖来源
mm audit — 最近的配置改动
mm add NVDA — 加入标的
mm add NVDA APP — 一次加多个
mm del APP — 移除标的
mm check CRWV — 只查不加
mm reset — 撤销全部覆盖
mm reset symbols — 只撤销一项

mm once — 跑一轮就退出
mm run — 前台常驻
mm verify — 校验并导出 CSV
mm demo — 离线演示

改的东西写进 config.local.toml，下一轮扫描自动生效，不用重启
"""


def _split_config(argv):
    """Extract --config anywhere; REMAINDER parsing would swallow it into the subcommand args."""
    rest, config = [], DEFAULT_CONFIG
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--config" and index + 1 < len(argv):
            config = argv[index + 1]
            index += 2
            continue
        if item.startswith("--config="):
            config = item.split("=", 1)[1]
            index += 1
            continue
        rest.append(item)
        index += 1
    return rest, config


ACTIONS = {"help", "status", "list", "ls", "add", "del", "delete", "rm",
           "check", "reset", "audit", "run", "once", "verify", "demo"}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    rest, config = _split_config(argv)
    if rest and rest[0] in {"-h", "--help"}:
        rest[0] = "help"
    if not rest:
        rest = ["status"]  # bare `mm` = 看一眼状态
    action, params = rest[0], rest[1:]
    if action == "config":  # legacy prefix: `config add` == `add`
        if not params:
            print(HELP)
            return 0
        action, params = params[0], params[1:]
    config_path = Path(config).expanduser().resolve()
    if action == "help":
        print(HELP)
        return 0
    if action not in ACTIONS:
        print(f"❌ 不认识的命令：{action}\n")
        print(HELP)
        return 1
    if not config_path.exists():
        print(f"❌ 找不到配置文件：{config_path}")
        return 1
    try:
        return _dispatch(action, params, config_path)
    except (ValueError, TypeError, OSError) as exc:
        # A malformed config.local.toml or a bad number must read as a message, not a traceback.
        print(f"❌ 配置有问题：{exc}")
        return 1


def _dispatch(action, params, config_path):
    if action == "status":
        return cmd_status(config_path)

    if action in {"list", "ls"}:
        return cmd_config_list(config_path)
    if action == "audit":
        return cmd_config_audit(config_path)
    if action == "add":
        if not params:
            print("用法：mm add SYMBOL [SYMBOL...]")
            return 1
        return cmd_config_add(config_path, params)
    if action in {"del", "delete", "rm"}:
        if not params:
            print("用法：mm del SYMBOL [SYMBOL...]")
            return 1
        return cmd_config_del(config_path, params)
    if action == "check":
        if not params:
            print("用法：mm check SYMBOL [SYMBOL...]")
            return 1
        return cmd_config_check(config_path, params)
    if action == "reset":
        return cmd_config_reset(config_path, params[0] if params else None)
    if action in {"run", "once", "verify", "demo"}:
        from .app import main as app_main
        return app_main([action, "--config", str(config_path), *params])
    print(f"❌ 不认识的命令：{action}")
    print(HELP)
    return 1