import json
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo
import math
import re
import tomllib

INTERVALS = {"1m": 60_000, "3m": 180_000, "5m": 300_000,
             "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}


# 已删除的历史配置项：仍被接受但不生效，避免旧 config.local.toml 让整轮扫描直接失败。
# sensitivity 与各阈值在数学上重复（它只是把 5 个阈值乘同一个系数，无法与分别调参区分）。
DEPRECATED_DETECTION = frozenset({"sensitivity", "er_min"})


@dataclass(frozen=True)
class Detection:
    atr_period: int = 14
    adx_period: int = 14
    fast_15_pct: float = 1.5
    fast_30_pct: float = 2.0
    fast_atr: float = 2.0
    fast_er_min: float = 0.65
    sustained_pct: float = 2.0
    sustained_atr: float = 2.0
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
    # 同一轮扫描里同向报警达到这个数量就合并成一条板块消息；0 或 1 表示不合并。
    merge_min_symbols: int = 3
    # 同一波行情里位移相对 episode 起点再扩大多少倍 ATR 时补一条升级提醒；0 关闭。
    escalation_atr: float = 1.5
    # 两条升级提醒之间至少间隔多少秒；0 关闭升级。不受 cooldown_seconds 约束，否则会被 60 分钟冷却吞掉。
    escalation_seconds: int = 1800
    # 每天自动清理一次超过这个天数的告警；0 关闭（仍可手动 mm prune）。
    retention_days: int = 180
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
        if type(self.merge_min_symbols) is not int or self.merge_min_symbols < 0:
            raise ValueError("merge_min_symbols must be a nonnegative integer")
        if not isinstance(self.escalation_atr, (int, float)) or self.escalation_atr < 0:
            raise ValueError("escalation_atr must be a nonnegative number")
        if type(self.escalation_seconds) is not int or self.escalation_seconds < 0:
            raise ValueError("escalation_seconds must be a nonnegative integer")
        if type(self.retention_days) is not int or self.retention_days < 0:
            raise ValueError("retention_days must be a nonnegative integer")
        d = self.detection
        for name, value in vars(d).items():
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid detection parameter: {name}")
        if any(type(p) is not int or p < 2 for p in (d.atr_period, d.adx_period)):
            raise ValueError("indicator periods must be integers >= 2")
        if d.quiet_er > 1 or d.fast_er_min > 1:
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
        if section == "detection":
            for key in list(value):
                if key in DEPRECATED_DETECTION:
                    value.pop(key)   # 旧覆盖里的已删除项：忽略，不报错
        _reject_unknown(value, section_type.__dataclass_fields__, f"[{section}]")
        raw[section] = section_type(**value)
    return Config(**raw)
