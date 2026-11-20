"""Device-independent measurements exchanged by acquisition plugins and sinks."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol, Sequence


_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def validate_id(value: str, field: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{field} must be 1-64 letters, digits, underscores or hyphens")


@dataclass(frozen=True)
class Measurement:
    time: datetime
    logger_id: str
    system_id: str
    instrument_id: str
    sensor_id: str
    native_value: Optional[float]
    native_unit: str
    value: Optional[float]
    unit: str
    quality: str = "good"
    conversion: Optional[str] = None

    def __post_init__(self) -> None:
        for field in ("logger_id", "system_id", "instrument_id", "sensor_id"):
            validate_id(getattr(self, field), field)
        if not isinstance(self.time, datetime) or self.time.tzinfo is None or self.time.utcoffset() is None:
            raise ValueError("measurement time must include a timezone")
        for field in ("native_unit", "unit"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a nonempty string")
        if self.conversion is not None and not isinstance(self.conversion, str):
            raise ValueError("conversion must be a string or None")
        if self.quality not in ("good", "invalid", "unavailable"):
            raise ValueError("unsupported measurement quality")
        for field in ("native_value", "value"):
            value = getattr(self, field)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{field} must be numeric or None")
                if not math.isfinite(value):
                    raise ValueError(f"{field} must be finite or None")
        if self.quality == "good" and self.value is None:
            raise ValueError("good measurements require a value")
        if self.quality != "good" and self.value is not None:
            raise ValueError("invalid or unavailable measurements cannot have a value")


class InstrumentPlugin(Protocol):
    """A plugin owns its hardware connection and returns timestamped measurements."""

    def open(self) -> None: ...

    def sample(self) -> Sequence[Measurement]: ...

    def close(self) -> None: ...
