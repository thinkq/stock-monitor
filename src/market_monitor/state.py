"""SQLite episode state and atomic notification reservation (at-most-once attempts)."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .config import Config
from .detection import Observation, Signal


@dataclass(frozen=True)
class Reserved:
    """一次成功预约的告警：alert id + 真正要推送的信号（升级时已补 escalation_from）。"""
    alert_id: int
    signal: Signal


class State:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS episodes (
                symbol TEXT, direction TEXT, active INTEGER DEFAULT 0,
                kind TEXT, quiet_count INTEGER DEFAULT 0, last_bar INTEGER DEFAULT -1,
                last_alert INTEGER, PRIMARY KEY(symbol, direction));
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, kind TEXT,
                detected_ms INTEGER, day TEXT, payload TEXT, status TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS pending_signals (
                symbol TEXT PRIMARY KEY, direction TEXT, first_seen INTEGER, last_seen INTEGER);
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS config_changes (
                id INTEGER PRIMARY KEY, ts TEXT, key TEXT, old_value TEXT,
                new_value TEXT, source TEXT);
        """)
        self._ensure_columns()

    def _ensure_columns(self):
        """老库升级：episodes 补上升级提醒需要的四列（CREATE TABLE IF NOT EXISTS 不会改老表）。"""
        existing = {row["name"] for row in self.db.execute("PRAGMA table_info(episodes)")}
        for name in ("origin_price", "origin_atr", "peak_atr", "announced_atr"):
            if name not in existing:
                self.db.execute(f"ALTER TABLE episodes ADD COLUMN {name} REAL DEFAULT 0")

    def close(self):
        self.db.close()

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except ValueError:
            return None

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
                        (key, json.dumps(value, ensure_ascii=False)))

    def log_config_change(self, key, old, new, source="cli"):
        self.db.execute(
            "INSERT INTO config_changes(ts,key,old_value,new_value,source) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), key,
             json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False), source))

    def recent_config_changes(self, limit=10):
        return self.db.execute(
            "SELECT ts,key,old_value,new_value,source FROM config_changes ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()

    def get_symbol_cache(self):
        """(fetched_at, trading symbol set) or None; corrupt values are ignored."""
        row = self.db.execute("SELECT value FROM meta WHERE key='active_symbols'").fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["value"])
            return float(data["fetched_at"]), set(map(str, data["symbols"]))
        except (ValueError, TypeError, KeyError):
            return None

    def set_symbol_cache(self, symbols, fetched_at):
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('active_symbols', ?)",
                        (json.dumps({"fetched_at": fetched_at, "symbols": sorted(symbols)}),))

    def clear_symbol_cache(self):
        self.db.execute("DELETE FROM meta WHERE key='active_symbols'")

    def clear_pending(self, symbol=None):
        """A failed observation interrupts confirmation, not an already alerted episode."""
        if symbol is None:
            self.db.execute("DELETE FROM pending_signals")
        else:
            self.db.execute("DELETE FROM pending_signals WHERE symbol=?", (symbol,))

    def _confirm_fast(self, symbol, direction, now_ms, config):
        """Confirmation survives restarts, but expires across missing scans."""
        row = self.db.execute("SELECT * FROM pending_signals WHERE symbol=?", (symbol,)).fetchone()
        if config.fast_confirmation_seconds == 0:
            self.db.execute("DELETE FROM pending_signals WHERE symbol=?", (symbol,))
            return True
        if row is None or row["direction"] != direction or not 0 <= now_ms - row["last_seen"] <= config.scan_seconds * 1500:
            self.db.execute("INSERT OR REPLACE INTO pending_signals VALUES (?,?,?,?)",
                            (symbol, direction, now_ms, now_ms))
            return False
        if now_ms - row["first_seen"] >= config.fast_confirmation_seconds * 1000:
            self.db.execute("DELETE FROM pending_signals WHERE symbol=?", (symbol,))
            return True
        self.db.execute("UPDATE pending_signals SET last_seen=? WHERE symbol=?", (now_ms, symbol))
        return False

    def _escalate(self, symbol, signal, row, now_ms, config):
        """同一波行情没结束、但位移明显扩大时补一条升级提醒。

        基准固定为 episode 起点价与报警时的 ATR：用当前 ATR 会让波动率扩张反过来压低倍数，
        升级时刻就变得不可解释。不受 cooldown_seconds 约束（30 分钟间隔已足够），只受日限约束。
        """
        if config.escalation_atr <= 0 or config.escalation_seconds <= 0:
            return None
        announced = row["announced_atr"] or 0.0
        origin_price, origin_atr = row["origin_price"] or 0.0, row["origin_atr"] or 0.0
        if announced <= 0 or origin_price <= 0 or origin_atr <= 0 or not signal.atr:
            return None          # 首报被限流挡过，或基准缺失：不升级
        sign = 1.0 if signal.direction == "up" else -1.0
        move = (signal.price - origin_price) * sign / origin_atr
        peak = max(row["peak_atr"] or 0.0, move)
        if (peak - announced < config.escalation_atr
                or now_ms - (row["last_alert"] or 0) < config.escalation_seconds * 1000):
            self.db.execute("UPDATE episodes SET peak_atr=? WHERE symbol=? AND direction=?",
                            (peak, symbol, signal.direction))
            return None
        day = datetime.fromtimestamp(now_ms / 1000, ZoneInfo(config.timezone)).date().isoformat()
        count = self.db.execute("SELECT count(*) FROM alerts WHERE day=? AND status != 'suppressed'",
                                (day,)).fetchone()[0]
        if count >= config.daily_limit:
            return None          # 日限满不落库也不推进档位，配额空了还能补
        # 消息里要显示"累计位移从 2.2 扩到 4.7"，所以 atr_units 换成累计值 peak。
        payload = replace(signal, kind="escalation",
                          escalation_from=float(announced), atr_units=peak)
        cursor = self.db.execute(
            "INSERT INTO alerts(symbol,direction,kind,detected_ms,day,payload,status,detail) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (symbol, signal.direction, "escalation", now_ms, day,
             json.dumps(asdict(payload), ensure_ascii=False), "reserved",
             f"from {announced:.2f}xATR"))
        self.db.execute("UPDATE episodes SET peak_atr=?,announced_atr=?,last_alert=?,kind='escalation' "
                        "WHERE symbol=? AND direction=?",
                        (peak, peak, now_ms, symbol, signal.direction))
        return Reserved(cursor.lastrowid, payload)

    def observe(self, symbol: str, obs: Observation, now_ms: int, config: Config):
        """Only call with fresh, fully validated data. Returns reserved alert id or None."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for direction in ("up", "down"):
                self.db.execute("INSERT OR IGNORE INTO episodes(symbol,direction) VALUES (?,?)", (symbol, direction))
                row = self.db.execute("SELECT * FROM episodes WHERE symbol=? AND direction=?", (symbol, direction)).fetchone()
                if obs.closed_time > row["last_bar"]:
                    consecutive = obs.closed_time - row["last_bar"] == config.interval_ms
                    count = (row["quiet_count"] if consecutive else 0) + 1 if obs.quiet else 0
                    active = row["active"] if count < config.reset_bars else 0
                    self.db.execute("UPDATE episodes SET active=?,quiet_count=?,last_bar=? WHERE symbol=? AND direction=?",
                                    (active, count, obs.closed_time, symbol, direction))
                elif not obs.quiet:
                    self.db.execute("UPDATE episodes SET quiet_count=0 WHERE symbol=? AND direction=?", (symbol, direction))
            signal = obs.signal
            alert_id = None
            if signal is None or signal.kind != "fast":
                self.db.execute("DELETE FROM pending_signals WHERE symbol=?", (symbol,))
            if signal:
                row = self.db.execute("SELECT * FROM episodes WHERE symbol=? AND direction=?", (symbol, signal.direction)).fetchone()
                if row["active"]:
                    self.db.execute("DELETE FROM pending_signals WHERE symbol=?", (symbol,))
                    if signal.kind == "sustained":
                        self.db.execute("UPDATE episodes SET kind='sustained' WHERE symbol=? AND direction=?", (symbol, signal.direction))
                    alert_id = self._escalate(symbol, signal, row, now_ms, config)
                else:
                    if signal.kind == "fast" and not self._confirm_fast(symbol, signal.direction, now_ms, config):
                        self.db.execute("COMMIT")
                        return None
                    day = datetime.fromtimestamp(now_ms / 1000, ZoneInfo(config.timezone)).date().isoformat()
                    count = self.db.execute("SELECT count(*) FROM alerts WHERE day=? AND status != 'suppressed'", (day,)).fetchone()[0]
                    cooling = row["last_alert"] is not None and now_ms - row["last_alert"] < config.cooldown_seconds * 1000
                    allowed = not cooling and count < config.daily_limit
                    status = "reserved" if allowed else "suppressed"
                    detail = "cooldown" if cooling else "daily_limit" if not allowed else None
                    cursor = self.db.execute("INSERT INTO alerts(symbol,direction,kind,detected_ms,day,payload,status,detail) VALUES (?,?,?,?,?,?,?,?)",
                                             (symbol, signal.direction, signal.kind, now_ms, day, json.dumps(asdict(signal), ensure_ascii=False), status, detail))
                    if allowed:
                        # 起点取"这次被报的那段位移的起点"，不是报警那一刻的价格：
                        # 否则 move 从 0 起算、announced 却是 atr_units，两边不同尺度，永远追不上。
                        sign = 1.0 if signal.direction == "up" else -1.0
                        origin = (signal.closes[0] if signal.closes
                                  else signal.price - sign * signal.atr_units * signal.atr)
                        self.db.execute(
                            "UPDATE episodes SET active=1,kind=?,quiet_count=0,last_alert=?,"
                            "origin_price=?,origin_atr=?,peak_atr=?,announced_atr=? "
                            "WHERE symbol=? AND direction=?",
                            (signal.kind, now_ms, origin, signal.atr,
                             signal.atr_units, signal.atr_units, symbol, signal.direction))
                        alert_id = Reserved(cursor.lastrowid, signal)
                    else:
                        self.db.execute("UPDATE episodes SET active=1,kind=?,quiet_count=0 "
                                        "WHERE symbol=? AND direction=?",
                                        (signal.kind, symbol, signal.direction))
            self.db.execute("COMMIT")
            return alert_id
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def finish(self, alert_id, status, detail=None):
        self.db.execute("UPDATE alerts SET status=?,detail=? WHERE id=?", (status, detail, alert_id))

    def maybe_prune(self, day: str, retention_days: int) -> int:
        """每天最多清理一次：删掉超过保留期的告警，避免库随扫描无限增长。"""
        if retention_days <= 0 or self.get_meta("last_prune_day") == day:
            return 0
        cutoff = (datetime.fromisoformat(day).date()
                  - timedelta(days=retention_days)).isoformat()
        removed = self.prune_alerts(cutoff)
        self.set_meta("last_prune_day", day)
        return removed

    def prune_alerts(self, cutoff_day: str) -> int:
        """Drop alerts older than cutoff_day (YYYY-MM-DD, inclusive of that day)."""
        return self.db.execute("DELETE FROM alerts WHERE day < ?", (cutoff_day,)).rowcount

    def prune_config_changes(self, keep: int = 200) -> int:
        return self.db.execute(
            "DELETE FROM config_changes WHERE id NOT IN "
            "(SELECT id FROM config_changes ORDER BY id DESC LIMIT ?)", (int(keep),)).rowcount

    def vacuum(self) -> None:
        # Deleting rows leaves pages allocated; only VACUUM returns space to the filesystem.
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.execute("VACUUM")
