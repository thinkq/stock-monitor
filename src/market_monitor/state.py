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
        """)

    def close(self):
        self.db.close()

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
            if signal:
                row = self.db.execute("SELECT * FROM episodes WHERE symbol=? AND direction=?", (symbol, signal.direction)).fetchone()
                if row["active"]:
                    if signal.kind == "sustained":
                        self.db.execute("UPDATE episodes SET kind='sustained' WHERE symbol=? AND direction=?", (symbol, signal.direction))
                else:
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
