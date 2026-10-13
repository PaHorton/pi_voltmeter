"""ADC backends. The app only depends on the ``Adc`` interface."""
from __future__ import annotations

import math
import time
from typing import Protocol

from .config import AdcConfig
from .settings import RANGE_CHOICES


class Adc(Protocol):
    def read_volts(self, channel: int, n: int, range_v: float) -> float:
        """Average of ``n`` single-ended samples on ``channel`` (0-3), in volts."""

    def close(self) -> None: ...


# ADS1115 registers / bit fields
_REG_CONV, _REG_CONFIG = 0x00, 0x01
_PGA_BITS = {rng: i for i, rng in enumerate(RANGE_CHOICES)}
_DR_128SPS = 0b100
_CONV_TIME_S = 1 / 128 + 0.001


class Ads1115:
    """Direct smbus2 driver (single-shot, single-ended, 128 SPS)."""

    def __init__(self, cfg: AdcConfig = AdcConfig()):
        from smbus2 import SMBus  # imported lazily so tests run off-Pi
        self._bus = SMBus(cfg.bus)
        self._addr = cfg.address

    def _sample(self, channel: int, range_v: float) -> float:
        config = (1 << 15) | ((0b100 | channel) << 12) | (_PGA_BITS[range_v] << 9) \
            | (1 << 8) | (_DR_128SPS << 5) | 0b11
        self._bus.write_i2c_block_data(self._addr, _REG_CONFIG, [config >> 8, config & 0xFF])
        deadline = time.monotonic() + 0.1
        time.sleep(_CONV_TIME_S)
        while time.monotonic() < deadline:
            hi, _lo = self._bus.read_i2c_block_data(self._addr, _REG_CONFIG, 2)
            if hi & 0x80:  # OS bit set => conversion done
                break
            time.sleep(0.001)
        hi, lo = self._bus.read_i2c_block_data(self._addr, _REG_CONV, 2)
        raw = (hi << 8) | lo
        if raw & 0x8000:
            raw -= 1 << 16
        return raw * range_v / 32768.0

    def read_volts(self, channel: int, n: int, range_v: float) -> float:
        return sum(self._sample(channel, range_v) for _ in range(n)) / n

    def close(self) -> None:
        self._bus.close()


class SimAdc:
    """Fake ADC for development off the Pi."""

    def read_volts(self, channel: int, n: int, range_v: float) -> float:
        t = time.monotonic()
        return 1.65 + 1.0 * math.sin(t * (0.5 + channel))

    def close(self) -> None:
        pass
