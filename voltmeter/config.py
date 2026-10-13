"""Device configuration (config.yaml): hardware addresses, pins, USB layout.

Separate from ``settings.py``: settings are user preferences changed from the
on-screen menu and rewritten at runtime; config is deployment-time, read-only.
Every key is optional and falls back to the defaults below.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List

import yaml

log = logging.getLogger(__name__)


@dataclass
class AdcConfig:
    bus: int = 1
    address: int = 0x48


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
    select: int = 4
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
class Config:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    # Reserved for sinks (e.g. TigerData host/port/table); passed through untouched.
    database: Dict[str, Any] = field(default_factory=dict)

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
        default = getattr(cls(), key)
        if is_dataclass(default):
            kwargs[key] = _build(type(default), value or {}, path)
        elif isinstance(default, dict):
            if not isinstance(value, dict):
                raise ValueError(f"'{path}' must be a mapping")
            kwargs[key] = value
        elif isinstance(default, list):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"'{path}' must be a list of strings")
            kwargs[key] = value
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"'{path}' must be an integer")
            kwargs[key] = value
        else:
            if not isinstance(value, str):
                raise ValueError(f"'{path}' must be a string")
            kwargs[key] = value
    return cls(**kwargs)
