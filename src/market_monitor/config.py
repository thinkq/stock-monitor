from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo
import math
import re
import tomllib

INTERVALS = {"1m": 60_000, "3m": 180_000, "5m": 300_000,
             "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}


@dataclass(frozen=True)
class Detection:
    sensitivity: float = 1.0
    atr_period: int = 14
    adx_period: int = 14
    fast_15_pct: float = 1.5
    fast_30_pct: float = 2.0
    fast_atr: float = 1.5
    sustained_pct: float = 2.0
    sustained_atr: float = 2.0
    er_min: float = 0.7
    pullback_atr: float = 0.35
    quiet_pct: float = 0.5
    quiet_er: float = 0.35


@dataclass(frozen=True)
class Telegram:
    enabled: bool = False
    token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"


@dataclass(frozen=True)
class Config:
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    interval: str = "15m"
    scan_seconds: int = 60
    history_bars: int = 200
    max_data_age_seconds: int = 180
    base_url: str = "https://fapi.binance.com"
    dns_over_https: bool = False
    database: str = "data/monitor.sqlite3"
    timezone: str = "Asia/Shanghai"
    directions: list[str] = field(default_factory=lambda: ["up", "down"])
    cooldown_seconds: int = 3600
    daily_limit: int = 30
    reset_bars: int = 2
    detection: Detection = field(default_factory=Detection)
    telegram: Telegram = field(default_factory=Telegram)

    def __post_init__(self):
        if type(self.dns_over_https) is not bool:
            raise ValueError("dns_over_https must be boolean")
        if self.interval not in INTERVALS:
            raise ValueError(f"interval must be one of {list(INTERVALS)}")
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be nonempty and unique")
        if any(not re.fullmatch(r"[A-Z0-9_]{3,30}", s) for s in self.symbols):
            raise ValueError("invalid symbol syntax")
        if not self.directions or not set(self.directions) <= {"up", "down"}:
            raise ValueError("directions must contain up/down")
        for name in ("scan_seconds", "max_data_age_seconds", "daily_limit", "reset_bars"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be nonnegative")
        d = self.detection
        for name, value in vars(d).items():
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid detection parameter: {name}")
        if any(type(p) is not int or p < 2 for p in (d.atr_period, d.adx_period)):
            raise ValueError("indicator periods must be integers >= 2")
        if d.er_min > 1 or d.quiet_er > 1:
            raise ValueError("ER thresholds must be <= 1")
        if not max(70, 3 * d.adx_period, d.atr_period + 10) <= self.history_bars <= 1000:
            raise ValueError("history_bars insufficient or > 1000")
        ZoneInfo(self.timezone)

    @property
    def interval_ms(self):
        return INTERVALS[self.interval]


def load(path: str) -> Config:
    with Path(path).open("rb") as f:
        raw = tomllib.load(f)
    raw["detection"] = Detection(**raw.get("detection", {}))
    raw["telegram"] = Telegram(**raw.get("telegram", {}))
    return Config(**raw)
