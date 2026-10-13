"""Persistent user settings (JSON), editable from the on-screen menu."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List

log = logging.getLogger(__name__)

N_CHOICES = [1, 2, 5, 10, 30, 60, 300, 600, 3600]  # N: seconds between timestamped records
RANGE_CHOICES = [6.144, 4.096, 2.048, 1.024, 0.512, 0.256]  # ADS1115 full scale, volts
PERIOD_CHOICES = [0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0]       # seconds between readings
NUM_CHANNELS = 2


@dataclass
class Settings:
    formulas: List[str] = field(default_factory=lambda: ["Raw"] * NUM_CHANNELS)
    n: int = 1
    range_v: float = 4.096
    period_s: float = 0.5
    usb_logging: bool = False

    def sanitize(self) -> "Settings":
        self.formulas = (list(self.formulas) + ["Raw"] * NUM_CHANNELS)[:NUM_CHANNELS]
        if self.n not in N_CHOICES:
            self.n = 1
        if self.range_v not in RANGE_CHOICES:
            self.range_v = 4.096
        if self.period_s not in PERIOD_CHOICES:
            self.period_s = 0.5
        self.usb_logging = bool(self.usb_logging)
        return self

    @classmethod
    def load(cls, path: Path) -> "Settings":
        try:
            data = json.loads(path.read_text())
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**known).sanitize()
        except FileNotFoundError:
            return cls()
        except (ValueError, TypeError, OSError) as exc:
            log.warning("ignoring bad settings file %s: %s", path, exc)
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(asdict(self), fh, indent=2)
        os.replace(tmp, path)  # atomic: power loss can't corrupt settings
