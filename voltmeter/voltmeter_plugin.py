"""ADS1115 acquisition with the existing voltmeter conversion semantics."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Callable, Dict, Sequence

from .adc import Adc
from .formulas import RAW, Formula
from .measurements import Measurement
from .settings import NUM_CHANNELS, Settings
from .sinks import ChannelReading, Reading

AVG_SAMPLES = 4


class VoltmeterPlugin:
    def __init__(self, adc: Adc, settings: Settings, divider: Sequence[float],
                 formulas: Callable[[], Dict[str, Formula]], logger_id: str,
                 system_id: str, instrument_id: str, sensor_ids: Sequence[str]):
        if len(divider) != NUM_CHANNELS or len(sensor_ids) != NUM_CHANNELS:
            raise ValueError("voltmeter requires two divider ratios and sensor IDs")
        self.adc, self.settings, self.divider, self.formulas = adc, settings, divider, formulas
        self.logger_id, self.system_id, self.instrument_id = logger_id, system_id, instrument_id
        self.sensor_ids = tuple(sensor_ids)

    def open(self) -> None:
        pass

    def sample_reading(self) -> Reading:
        channels = []
        formulas = self.formulas()
        for ch in range(NUM_CHANNELS):
            formula = formulas.get(self.settings.formulas[ch], RAW)
            volts = self.adc.read_volts(ch, AVG_SAMPLES, self.settings.range_v) * self.divider[ch]
            channels.append(ChannelReading(volts, formula(volts), formula.unit, formula.name))
        return Reading(datetime.now().astimezone(), channels)

    def sample(self) -> Sequence[Measurement]:
        return self.measurements(self.sample_reading())

    def measurements(self, reading: Reading) -> Sequence[Measurement]:
        formulas = self.formulas()
        measurements = []
        for sensor_id, channel in zip(self.sensor_ids, reading.channels):
            valid = math.isfinite(channel.volts) and math.isfinite(channel.value)
            formula = formulas.get(channel.formula, RAW)
            measurements.append(Measurement(
                time=reading.timestamp, logger_id=self.logger_id, system_id=self.system_id,
                instrument_id=self.instrument_id, sensor_id=sensor_id,
                native_value=channel.volts if math.isfinite(channel.volts) else None,
                native_unit="V", value=channel.value if valid else None, unit=channel.unit,
                quality="good" if valid else "invalid",
                conversion=f"{formula.name}: {formula.expression}"))
        return measurements

    def close(self) -> None:
        self.adc.close()
