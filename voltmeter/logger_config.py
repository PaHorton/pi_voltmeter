"""Configuration for the opt-in lab logger process."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

import yaml

from .measurements import validate_id
from .catalog import System


@dataclass(frozen=True)
class InstrumentConfig:
    id: str
    type: str
    system_id: str
    interval_s: float
    sensors: Dict[int, str]
    options: Dict[str, Any]


@dataclass(frozen=True)
class LoggerConfig:
    logger_id: str
    instruments: Tuple[InstrumentConfig, ...]


def load_logger_config(path: Path) -> LoggerConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"version", "logger_id", "instruments"}:
        raise ValueError("logger configuration requires version, logger_id and instruments")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("unsupported logger configuration version")
    validate_id(data["logger_id"], "logger_id")
    if not isinstance(data["instruments"], list):
        raise ValueError("instruments must be a list")
    instruments = []
    seen = set()
    sensor_keys = set()
    for item in data["instruments"]:
        keys = {"id", "type", "system_id", "interval_s", "sensors", "options"}
        if not isinstance(item, dict) or set(item) != keys:
            raise ValueError("instrument requires id, type, system_id, interval_s, sensors and options")
        for field in ("id", "system_id"):
            validate_id(item[field], field)
        if item["id"] in seen:
            raise ValueError(f"duplicate instrument id: {item['id']}")
        seen.add(item["id"])
        if item["type"] not in ("voltmeter", "lakeshore218"):
            raise ValueError(f"unknown plugin type: {item['type']}")
        interval = item["interval_s"]
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not math.isfinite(interval) or interval <= 0):
            raise ValueError("interval_s must be finite and positive")
        if not isinstance(item["sensors"], dict) or not item["sensors"]:
            raise ValueError("sensors must be a nonempty channel-to-ID mapping")
        for channel, sensor_id in item["sensors"].items():
            allowed = (0, 1) if item["type"] == "voltmeter" else range(1, 9)
            if type(channel) is not int or channel not in allowed:
                raise ValueError(f"invalid channel for {item['type']}: {channel}")
            validate_id(sensor_id, "sensor id")
            key = (item["system_id"], sensor_id)
            if key in sensor_keys:
                raise ValueError(f"duplicate sensor id in system: {sensor_id}")
            sensor_keys.add(key)
        if not isinstance(item["options"], dict):
            raise ValueError("plugin options must be a mapping")
        instruments.append(InstrumentConfig(item["id"], item["type"], item["system_id"],
                                             float(interval), dict(item["sensors"]),
                                             dict(item["options"])))
    return LoggerConfig(data["logger_id"], tuple(instruments))


def validate_catalog(config: LoggerConfig, systems: Dict[str, System]) -> None:
    """Reject measurements that would not have a corresponding public sensor."""
    for instrument in config.instruments:
        system = systems.get(instrument.system_id)
        if system is None:
            raise ValueError(f"unknown system: {instrument.system_id}")
        sensors = {sensor.id: sensor for sensor in system.sensors}
        for sensor_id in instrument.sensors.values():
            sensor = sensors.get(sensor_id)
            if sensor is None or sensor.instrument_id != instrument.id:
                raise ValueError(f"sensor {sensor_id} is not registered for {instrument.id}")
