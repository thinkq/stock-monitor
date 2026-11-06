"""SQLite episode state and atomic notification reservation (at-most-once attempts)."""
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .config import Config
from .detection import Observation


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
                    self.db.execute("UPDATE episodes SET active=1,kind=?,quiet_count=0,last_alert=? WHERE symbol=? AND direction=?",
                                    (signal.kind, now_ms if allowed else row["last_alert"], symbol, signal.direction))
                    if allowed:
                        alert_id = cursor.lastrowid
            self.db.execute("COMMIT")
            return alert_id
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def finish(self, alert_id, status, detail=None):
        self.db.execute("UPDATE alerts SET status=?,detail=? WHERE id=?", (status, detail, alert_id))
