import json
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
    fast_atr: float = 2.0
    fast_er_min: float = 0.65
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
    fast_confirmation_seconds: int = 60
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
        if type(self.fast_confirmation_seconds) is not int or self.fast_confirmation_seconds < 0:
            raise ValueError("fast_confirmation_seconds must be a nonnegative integer")
        d = self.detection
        for name, value in vars(d).items():
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid detection parameter: {name}")
        if any(type(p) is not int or p < 2 for p in (d.atr_period, d.adx_period)):
            raise ValueError("indicator periods must be integers >= 2")
        if d.er_min > 1 or d.quiet_er > 1 or d.fast_er_min > 1:
            raise ValueError("ER thresholds must be <= 1")
        if not max(70, 3 * d.adx_period, d.atr_period + 10) <= self.history_bars <= 1000:
            raise ValueError("history_bars insufficient or > 1000")
        ZoneInfo(self.timezone)

    @property
    def interval_ms(self):
        return INTERVALS[self.interval]


def local_path(path: str | Path) -> Path:
    """config.toml → config.local.toml: the overlay chat edits are written to."""
    base = Path(path)
    return base.with_name(base.stem + ".local.toml")


def read_base(path: str | Path) -> dict:
    with Path(path).open("rb") as f:
        return tomllib.load(f)


def load_overrides(path: str | Path) -> dict:
    overlay = local_path(path)
    if not overlay.exists():
        return {}
    return read_base(overlay)


def _merge(base: dict, overlay: dict) -> dict:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ValueError(f"unsupported overlay value: {value!r}")


def dump_overrides(overrides: dict) -> str:
    """Serialize the overlay file: scalars first, then tables (TOML requirement)."""
    scalars = {k: v for k, v in overrides.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in overrides.items() if isinstance(v, dict)}
    lines = ["# 由 market-monitor config 写入；删除本文件即回到 config.toml 的值。", ""]
    lines += [f"{key} = {_toml_value(value)}" for key, value in scalars.items()]
    for name, table in tables.items():
        lines += ["", f"[{name}]"]
        lines += [f"{key} = {_toml_value(value)}" for key, value in table.items()]
    return "\n".join(lines).rstrip() + "\n"


def save_overrides(path: str | Path, overrides: dict) -> Path:
    overlay = local_path(path)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(dump_overrides(overrides), encoding="utf-8")
    return overlay


def _reject_unknown(raw: dict, allowed, where: str) -> None:
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise ValueError(f"{where} 有未知配置项：{', '.join(unknown)}")


def load(path: str | Path) -> Config:
    raw = _merge(read_base(path), load_overrides(path))
    _reject_unknown(raw, Config.__dataclass_fields__, f"{Path(path).name} / {local_path(path).name}")
    for section, section_type in (("detection", Detection), ("telegram", Telegram)):
        value = raw.get(section, {})
        if not isinstance(value, dict):
            raise ValueError(f"[{section}] 必须是表")
        _reject_unknown(value, section_type.__dataclass_fields__, f"[{section}]")
        raw[section] = section_type(**value)
    return Config(**raw)
