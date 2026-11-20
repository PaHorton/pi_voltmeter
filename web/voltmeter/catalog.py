"""Public, file-defined systems and sensors for lab dashboards."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

import yaml

from .measurements import validate_id


@dataclass(frozen=True)
class Sensor:
    id: str
    label: str
    instrument_id: str
    unit: str
    stale_after_s: float


@dataclass(frozen=True)
class System:
    id: str
    label: str
    sensors: Tuple[Sensor, ...]


def load_catalog(path: Path) -> Dict[str, System]:
    """Load explicitly published metadata; invalid definitions fail at startup."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"version", "systems"} or data["version"] != 1:
        raise ValueError("catalog must contain version: 1 and systems")
    if not isinstance(data["systems"], list):
        raise ValueError("systems must be a list")
    systems: Dict[str, System] = {}
    for entry in data["systems"]:
        if not isinstance(entry, dict) or set(entry) != {"id", "label", "sensors"}:
            raise ValueError("each system must contain id, label and sensors")
        system_id = entry["id"]
        validate_id(system_id, "system id")
        if system_id in systems:
            raise ValueError(f"duplicate system id: {system_id}")
        if not isinstance(entry["label"], str) or not entry["label"].strip():
            raise ValueError("system label must be nonempty")
        if not isinstance(entry["sensors"], list):
            raise ValueError("sensors must be a list")
        sensors = []
        seen = set()
        for item in entry["sensors"]:
            if not isinstance(item, dict) or set(item) != {
                "id", "label", "instrument_id", "unit", "stale_after_s"
            }:
                raise ValueError("sensor requires id, label, instrument_id, unit and stale_after_s")
            validate_id(item["id"], "sensor id")
            validate_id(item["instrument_id"], "instrument id")
            if item["id"] in seen:
                raise ValueError(f"duplicate sensor id in {system_id}: {item['id']}")
            seen.add(item["id"])
            if any(not isinstance(item[k], str) or not item[k].strip() for k in ("label", "unit")):
                raise ValueError("sensor label and unit must be nonempty")
            stale = item["stale_after_s"]
            if isinstance(stale, bool) or not isinstance(stale, (int, float)) or not 0 < stale <= 604800:
                raise ValueError("stale_after_s must be a number between 0 and 604800")
            sensors.append(Sensor(item["id"], item["label"], item["instrument_id"],
                                  item["unit"], float(stale)))
        systems[system_id] = System(system_id, entry["label"], tuple(sensors))
    return systems
