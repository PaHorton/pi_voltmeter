"""Publish readings to TigerData (Postgres + TimescaleDB).

``write()`` only appends to an in-memory buffer, so the sampling loop and display never
block on the network. A worker thread batches rows into the database, retrying with
backoff while offline. Rows are idempotent (primary key + ON CONFLICT DO NOTHING), so a
retry after an ambiguous failure cannot create duplicates.

Schema (one row per channel per record; Grafana-friendly "long" format)::

    time timestamptz, device_id text, channel smallint,
    volts double precision, value double precision, unit text, formula text
"""
from __future__ import annotations

import logging
import math
import os
import re
import socket
import threading
from collections import deque
from typing import Any, Callable, Deque, List, Optional, Tuple

from .config import DatabaseConfig
from .sinks import Reading

log = logging.getLogger(__name__)

Row = Tuple[Any, ...]
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_MAX_BACKOFF_S = 60.0


class TigerDataSink:
    def __init__(self, cfg: DatabaseConfig, connect: Callable[[], Any], device_id: str):
        if not _IDENT.match(cfg.table):
            raise ValueError(f"database.table '{cfg.table}' is not a plain identifier")
        self._cfg = cfg
        self._connect = connect
        self.device_id = device_id
        self._buf: Deque[Row] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._conn: Any = None
        self._dropped = 0
        self._insert_sql = (
            f"INSERT INTO {cfg.table} (time, device_id, channel, volts, value, unit, formula) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING")
        self._thread = threading.Thread(target=self._run, name="tigerdata", daemon=True)
        self._thread.start()

    # ---- Sink interface ----------------------------------------------
    def write(self, reading: Reading) -> None:
        rows = [(reading.timestamp, self.device_id, i, _num(c.volts), _num(c.value),
                 c.unit, c.formula) for i, c in enumerate(reading.channels)]
        with self._lock:
            self._buf.extend(rows)
            self._trim()
            ready = len(self._buf) >= self._cfg.batch_size
        if ready:
            self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=10)
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - best effort on shutdown
                pass

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._buf)

    # ---- worker ------------------------------------------------------
    def _trim(self) -> None:
        """Caller holds the lock. Drop oldest rows beyond max_buffer."""
        excess = len(self._buf) - self._cfg.max_buffer
        if excess > 0:
            for _ in range(excess):
                self._buf.popleft()
            if self._dropped == 0:
                log.warning("tigerdata buffer full; dropping oldest rows")
            self._dropped += excess

    def _take(self) -> List[Row]:
        with self._lock:
            n = min(self._cfg.batch_size, len(self._buf))
            return [self._buf.popleft() for _ in range(n)]

    def _give_back(self, batch: List[Row]) -> None:
        with self._lock:
            self._buf.extendleft(reversed(batch))
            self._trim()

    def _ensure_connection(self) -> None:
        if self._conn is None:
            conn = self._connect()
            try:
                self._init_schema(conn)
            except Exception:
                conn.close()
                raise
            self._conn = conn
            log.info("connected to TigerData (%s)", self._cfg.host)

    def _init_schema(self, conn: Any) -> None:
        t = self._cfg.table
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {t} ("
                "time timestamptz NOT NULL, device_id text NOT NULL, channel smallint NOT NULL, "
                "volts double precision, value double precision, unit text, formula text, "
                "PRIMARY KEY (device_id, channel, time))")
        conn.commit()
        try:  # plain Postgres has no hypertables; TigerData does
            with conn.cursor() as cur:
                cur.execute(f"SELECT create_hypertable('{t}', 'time', if_not_exists => TRUE)")
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            log.warning("could not create hypertable (plain table in use): %s", exc)

    def _flush_once(self) -> bool:
        """Send one batch. Returns True if sent or nothing to send."""
        self._ensure_connection()
        batch = self._take()
        if not batch:
            return True
        try:
            with self._conn.cursor() as cur:
                cur.executemany(self._insert_sql, batch)
            self._conn.commit()
        except Exception:
            self._give_back(batch)
            raise
        return True

    def _drain(self) -> None:
        while self.pending and not self._stop.is_set():
            self._flush_once()

    def _run(self) -> None:
        backoff, failing = 1.0, False
        while not self._stop.is_set():
            self._wake.wait(timeout=self._cfg.flush_interval_s)
            self._wake.clear()
            try:
                self._drain()
                if failing:
                    log.info("tigerdata connection recovered")
                backoff, failing = 1.0, False
            except Exception as exc:  # noqa: BLE001 - network/db errors of any type
                if not failing:
                    log.warning("tigerdata unavailable (%s); buffering %d rows",
                                exc, self.pending)
                failing = True
                self._reset_connection()
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
        # final best-effort flush so a clean shutdown loses nothing
        try:
            while self.pending:
                self._flush_once()
        except Exception as exc:  # noqa: BLE001
            log.warning("tigerdata final flush failed, %d rows lost: %s", self.pending, exc)

    def _reset_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None


def _num(x: float) -> Optional[float]:
    """NaN/inf -> NULL so dashboards show a gap instead of a bogus point."""
    return x if math.isfinite(x) else None


def build_sink(cfg: DatabaseConfig) -> TigerDataSink:
    """Validate config and create the sink. Raises ValueError with an actionable message."""
    if not cfg.host:
        raise ValueError("database.enabled is true but database.host is empty")
    password = os.environ.get(cfg.password_env)
    if not password:
        raise ValueError(f"set the database password in ${cfg.password_env}")
    try:
        import psycopg
    except ImportError as exc:
        raise ValueError("psycopg not installed: pip install '.[tigerdata]'") from exc

    def connect():
        return psycopg.connect(host=cfg.host, port=cfg.port, dbname=cfg.dbname, user=cfg.user,
                               password=password, sslmode=cfg.sslmode, connect_timeout=10)

    return TigerDataSink(cfg, connect, cfg.device_id or socket.gethostname())
