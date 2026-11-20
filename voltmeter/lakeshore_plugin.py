"""Adapt a read-only Lake Shore monitor to generic measurements."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Sequence

from .lakeshore218 import LakeShore218
from .measurements import Measurement

log = logging.getLogger(__name__)


class LakeShorePlugin:
    def __init__(self, factory: Callable[[], LakeShore218], logger_id: str,
                 system_id: str, instrument_id: str, sensors: Dict[int, str]):
        if not sensors or any(type(channel) is not int or not 1 <= channel <= 8
                              for channel in sensors):
            raise ValueError("Lake Shore inputs must be integers 1 through 8")
        self.factory = factory
        self.logger_id, self.system_id, self.instrument_id = logger_id, system_id, instrument_id
        self.sensors = dict(sensors)
        self._driver: Optional[LakeShore218] = None

    def open(self) -> None:
        if self._driver is not None:
            raise RuntimeError("Lake Shore plugin is already open")
        self._driver = self.factory()

    def sample(self) -> Sequence[Measurement]:
        if self._driver is None:
            raise RuntimeError("Lake Shore plugin is not open")
        result = []
        for channel, sensor_id in self.sensors.items():
            reading = self._driver.read_kelvin(channel)
            if not reading.valid:
                log.warning("Lake Shore %s input %d invalid: %s", self.instrument_id, channel, reading.error)
            result.append(Measurement(
                time=datetime.now(timezone.utc), logger_id=self.logger_id,
                system_id=self.system_id, instrument_id=self.instrument_id, sensor_id=sensor_id,
                native_value=reading.kelvin, native_unit="K",
                value=reading.kelvin if reading.valid else None, unit="K",
                quality="good" if reading.valid else "invalid",
                conversion=None))
        return result

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.close()
            finally:
                self._driver = None
