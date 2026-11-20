"""Independent workers for instrument plugins."""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from typing import Callable, Sequence

from .measurements import InstrumentPlugin, Measurement

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginSchedule:
    name: str
    plugin: InstrumentPlugin
    interval_s: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.interval_s) or self.interval_s <= 0:
            raise ValueError("plugin interval must be finite and positive")


class PluginRuntime:
    """Run each plugin on its own thread so an unavailable instrument is isolated."""

    def __init__(self, schedules: Sequence[PluginSchedule],
                 publish: Callable[[Sequence[Measurement]], None]):
        names = [schedule.name for schedule in schedules]
        if len(set(names)) != len(names):
            raise ValueError("plugin names must be unique")
        self._schedules = tuple(schedules)
        self._publish = publish
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("runtime already started")
        for schedule in self._schedules:
            thread = threading.Thread(target=self._worker, args=(schedule,),
                                      name=f"instrument-{schedule.name}", daemon=True)
            self._threads.append(thread)
            thread.start()

    def _worker(self, schedule: PluginSchedule) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                schedule.plugin.open()
                while not self._stop.is_set():
                    self._publish(schedule.plugin.sample())
                    backoff = 1.0
                    self._stop.wait(schedule.interval_s)
            except Exception:
                log.exception("plugin %s failed; retrying", schedule.name)
            finally:
                try:
                    schedule.plugin.close()
                except Exception:
                    log.exception("plugin %s close failed", schedule.name)
            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)

    def close(self, timeout: float = 10.0) -> None:
        self._stop.set()
        alive = []
        for thread in self._threads:
            thread.join(timeout)
            if thread.is_alive():
                alive.append(thread.name)
        if alive:
            raise TimeoutError(f"plugin workers did not stop: {', '.join(alive)}")
