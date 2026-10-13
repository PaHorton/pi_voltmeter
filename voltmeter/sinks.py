"""Reading data model and output sinks.

Anything that consumes readings (USB CSV now; TigerData/Grafana uploader later)
implements ``Sink``. To add one, write a class with ``write``/``close`` and
register it with ``App.add_sink``.
"""
from __future__ import annotations

import csv
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, List, Optional, Protocol


@dataclass(frozen=True)
class ChannelReading:
    volts: float
    value: float
    unit: str
    formula: str


@dataclass(frozen=True)
class Reading:
    timestamp: datetime
    channels: List[ChannelReading]


class Sink(Protocol):
    def write(self, reading: Reading) -> None: ...
    def close(self) -> None: ...


class CsvSink:
    """Appends readings to a new timestamped CSV file inside ``folder``."""

    def __init__(self, folder: Path, num_channels: int):
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = folder / f"log_{stamp}.csv"
        self._fh: Optional[IO[str]] = open(self.path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        header = ["timestamp"]
        for i in range(num_channels):
            header += [f"ch{i}_volts", f"ch{i}_value", f"ch{i}_unit", f"ch{i}_formula"]
        self._writer.writerow(header)
        self._last_sync = 0.0

    def write(self, reading: Reading) -> None:
        if self._fh is None:
            return
        row = [reading.timestamp.isoformat(timespec="milliseconds")]
        for ch in reading.channels:
            row += [f"{ch.volts:.6f}", f"{ch.value:.6g}", ch.unit, ch.formula]
        self._writer.writerow(row)
        self._fh.flush()
        now = time.monotonic()
        if now - self._last_sync > 1.0:  # bound data loss on yank without fsync every row
            os.fsync(self._fh.fileno())
            self._last_sync = now

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except OSError:
                pass  # stick already removed
            finally:
                self._fh.close()
                self._fh = None
