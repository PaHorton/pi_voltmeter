"""Opt-in buffered storage for Measurement objects.

Run db/lab_measurements.sql as an administrator before using this sink. Pass a
zero-argument DB-API connection factory (for example one calling psycopg.connect)
and call write() for each Measurement. The sink owns and closes its connections.
The bounded in-memory queue is not durable; overflow drops its oldest entries.
close() stops accepting writes and waits for a final best-effort drain. It can
block on database I/O, so configure connection and statement timeouts separately.
"""
from __future__ import annotations

import logging
import math
import threading
from collections import deque
from itertools import islice
from typing import Any, Callable, Deque, Tuple

from .measurements import Measurement

log = logging.getLogger(__name__)

_INSERT_SQL = (
    "INSERT INTO public.lab_measurements "
    "(time, logger_id, system_id, instrument_id, sensor_id, native_value, "
    "native_unit, value, unit, quality, conversion) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (time, logger_id, system_id, instrument_id, sensor_id) DO NOTHING"
)


class MeasurementSink:
    """Asynchronously batch measurements into the pre-created hypertable."""

    def __init__(
        self,
        connect: Callable[[], Any],
        *,
        batch_size: int = 100,
        max_buffer: int = 10000,
        flush_interval_s: float = 1.0,
        retry_initial_s: float = 1.0,
        retry_max_s: float = 60.0,
    ) -> None:
        for name, value in (("batch_size", batch_size), ("max_buffer", max_buffer)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name, value in (("flush_interval_s", flush_interval_s),
                            ("retry_initial_s", retry_initial_s),
                            ("retry_max_s", retry_max_s)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if retry_initial_s > retry_max_s:
            raise ValueError("retry_initial_s cannot exceed retry_max_s")
        self._connect = connect
        self._batch_size = batch_size
        self._max_buffer = max_buffer
        self._flush_interval = flush_interval_s
        self._retry_initial = retry_initial_s
        self._retry_max = retry_max_s
        self._buffer: Deque[Tuple[int, Measurement]] = deque()
        self._next_id = 0
        self._dropped = 0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._closed = False
        self._conn: Any = None
        self._thread = threading.Thread(target=self._run, name="measurement-sink", daemon=True)
        self._thread.start()

    def write(self, measurement: Measurement) -> None:
        """Queue one measurement without performing database I/O."""
        if not isinstance(measurement, Measurement):
            raise TypeError("measurement must be a Measurement")
        with self._lock:
            if self._closed:
                raise RuntimeError("measurement sink is closed")
            self._buffer.append((self._next_id, measurement))
            self._next_id += 1
            if len(self._buffer) > self._max_buffer:
                self._buffer.popleft()
                self._dropped += 1
                log.warning("measurement buffer full; dropped oldest row (total dropped: %d)",
                            self._dropped)
            if len(self._buffer) >= self._batch_size:
                self._wake.set()

    @property
    def pending(self) -> int:
        """Number of queued rows, including any batch currently being sent."""
        with self._lock:
            return len(self._buffer)

    @property
    def dropped(self) -> int:
        """Number of rows evicted due to buffer overflow."""
        with self._lock:
            return self._dropped

    def close(self) -> None:
        """Stop the worker after one best-effort drain; report losses in the log."""
        with self._lock:
            self._closed = True
            self._stop.set()
            self._wake.set()
        self._thread.join()

    def _reset_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                log.exception("failed to close measurement database connection")
            self._conn = None

    def _flush_once(self) -> None:
        with self._lock:
            batch = list(islice(self._buffer, self._batch_size))
        if not batch:
            return
        if self._conn is None:
            self._conn = self._connect()
        rows = [
            (m.time, m.logger_id, m.system_id, m.instrument_id, m.sensor_id,
             m.native_value, m.native_unit, m.value, m.unit, m.quality, m.conversion)
            for _, m in batch
        ]
        with self._conn.cursor() as cur:
            cur.executemany(_INSERT_SQL, rows)
        self._conn.commit()
        last_id = batch[-1][0]
        with self._lock:
            while self._buffer and self._buffer[0][0] <= last_id:
                self._buffer.popleft()

    def _run(self) -> None:
        backoff = self._retry_initial
        failing = False
        try:
            while not self._stop.is_set():
                self._wake.wait(self._flush_interval)
                self._wake.clear()
                try:
                    while self.pending and not self._stop.is_set():
                        self._flush_once()
                    if failing and not self._stop.is_set():
                        log.info("measurement database connection recovered")
                        failing = False
                    backoff = self._retry_initial
                except Exception as exc:
                    if not failing:
                        log.warning("measurement database unavailable (%s); buffering %d rows",
                                    exc, self.pending)
                    failing = True
                    self._reset_connection()
                    if not self._stop.wait(backoff):
                        self._wake.set()
                    backoff = min(backoff * 2, self._retry_max)
            try:
                while self.pending:
                    self._flush_once()
            except Exception as exc:
                log.warning("measurement final flush failed; %d rows lost: %s", self.pending, exc)
        finally:
            self._reset_connection()
