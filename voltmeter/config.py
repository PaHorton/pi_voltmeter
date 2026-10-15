"""Device configuration (config.yaml): hardware addresses, pins, USB layout.

Separate from ``settings.py``: settings are user preferences changed from the
on-screen menu and rewritten at runtime; config is deployment-time, read-only.
Every key is optional and falls back to the defaults below.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, List, Optional

import yaml

log = logging.getLogger(__name__)


@dataclass
class DividerConfig:
    """Resistor divider ahead of an ADC input: signal -- r_top -- ADC pin -- r_bottom -- GND.

    The default (r_top 0) means no divider. Units cancel, so use ohms or kohms.
    """
    r_top: float = 0.0
    r_bottom: float = 1.0

    @property
    def ratio(self) -> float:
        """Multiply the voltage at the ADC pin by this to get the input voltage."""
        if self.r_top < 0 or self.r_bottom <= 0:
            raise ValueError("divider: r_top must be >= 0 and r_bottom > 0")
        return (self.r_top + self.r_bottom) / self.r_bottom


@dataclass
class AdcConfig:
    bus: int = 1
    address: int = 0x48
    ch0_divider: DividerConfig = field(default_factory=DividerConfig)
    ch1_divider: DividerConfig = field(default_factory=DividerConfig)

    def ratios(self) -> list:
        return [self.ch0_divider.ratio, self.ch1_divider.ratio]


@dataclass
class DisplayConfig:
    bus: int = 1
    address: int = 0x3C


@dataclass
class ButtonsConfig:
    """BCM GPIO numbers; defaults match the Adafruit 128x64 OLED bonnet."""
    up: int = 17
    down: int = 22
    left: int = 27
    right: int = 23
    select: Optional[int] = field(default=4, metadata={"nullable": True})  # null disables
    a: int = 5
    b: int = 6


@dataclass
class UsbConfig:
    mount_roots: List[str] = field(default_factory=lambda: ["/media/", "/mnt/", "/run/media/"])
    device_prefix: str = "/dev/sd"
    formulas_dir: str = "formulas"      # folder on the stick to import formulas from
    log_dir: str = "voltmeter_logs"     # folder on the stick CSV logs are written to


@dataclass
class DeviceConfig:
    adc: AdcConfig = field(default_factory=AdcConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    buttons: ButtonsConfig = field(default_factory=ButtonsConfig)
    usb: UsbConfig = field(default_factory=UsbConfig)


@dataclass
class DatabaseConfig:
    """TigerData (Postgres + TimescaleDB). The password is never stored here: it is read
    from the environment variable named by ``password_env``."""
    enabled: bool = False
    host: str = ""
    port: int = 5432
    dbname: str = "tsdb"
    user: str = "tsdbadmin"
    password_env: str = "VOLTMETER_DB_PASSWORD"
    sslmode: str = "require"
    table: str = "readings"
    device_id: str = ""             # defaults to the hostname when empty
    batch_size: int = 50            # rows per INSERT
    flush_interval_s: float = 5.0   # max wait before sending a partial batch
    max_buffer: int = 100000        # rows held in memory while offline; oldest dropped beyond


@dataclass
class Config:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)

    @classmethod
    def load(cls, path: Path) -> "Config":
        """Missing file -> defaults. Malformed file raises ValueError (fail loudly at startup)."""
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.info("no config at %s; using defaults", path)
            return cls()
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML in {path}: {exc}") from exc
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: top level must be a mapping")
        try:
            return _build(cls, raw, "")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}: {exc}") from exc


def _build(cls, data: Any, where: str):
    if not isinstance(data, dict):
        raise ValueError(f"'{where}' must be a mapping")
    known = {f.name: f for f in fields(cls)}
    kwargs = {}
    for key, value in data.items():
        path = f"{where}.{key}" if where else key
        if key not in known:
            log.warning("config: ignoring unknown key '%s'", path)
            continue
        if value is None and known[key].metadata.get("nullable"):
            kwargs[key] = None
            continue
        default = getattr(cls(), key)
        if is_dataclass(default):
            kwargs[key] = _build(type(default), value or {}, path)
        elif isinstance(default, list):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"'{path}' must be a list of strings")
            kwargs[key] = value
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"'{path}' must be true or false")
            kwargs[key] = value
        elif isinstance(default, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"'{path}' must be a number")
            kwargs[key] = float(value)
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"'{path}' must be an integer")
            kwargs[key] = value
        else:
            if not isinstance(value, str):
                raise ValueError(f"'{path}' must be a string")
            kwargs[key] = value
    return cls(**kwargs)
