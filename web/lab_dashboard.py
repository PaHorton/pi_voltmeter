"""Opt-in public system dashboard and scoped, read-only measurement API."""
from __future__ import annotations

import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from voltmeter.catalog import Sensor, System, load_catalog


HERE = Path(__file__).resolve().parent


def _load_public_catalog() -> Dict[str, System]:
    return load_catalog(HERE / os.environ.get("LAB_CATALOG", "catalog.yaml"))


CATALOG = _load_public_catalog()
PAGE = (HERE / "lab.html").read_text(encoding="utf-8")
router = APIRouter()

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RANGES = {
    "1m": 60, "5m": 300, "10m": 600, "30m": 1800,
    "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800,
}
_TARGET_POINTS = 300
_MAX_HISTORY_POINTS = 500


def demo_enabled() -> bool:
    return os.environ.get("DEMO_MODE", "").lower() in {"1", "true", "yes"}


def _table() -> str:
    qualified = os.environ.get("LAB_MEASUREMENTS_TABLE", "public.lab_measurements")
    parts = qualified.split(".")
    if not 1 <= len(parts) <= 2 or any(not _IDENT.fullmatch(part) for part in parts):
        raise ValueError("LAB_MEASUREMENTS_TABLE must be a plain table identifier")
    return ".".join(parts)


def _connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    import psycopg

    return psycopg.connect(url, connect_timeout=5,
                           options="-c default_transaction_read_only=on")


def _system_or_404(system_id: str) -> System:
    system = CATALOG.get(system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="system not found")
    return system


def _sensor_or_404(system: System, sensor_id: str) -> Sensor:
    for sensor in system.sensors:
        if sensor.id == sensor_id:
            return sensor
    raise HTTPException(status_code=404, detail="sensor not found")


def _number(value: Any):
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def fetch_latest(system: System) -> List[Dict[str, Any]]:
    sensors = [sensor.id for sensor in system.sensors]
    if not sensors:
        return []
    sql = (
        f"SELECT DISTINCT ON (sensor_id) time, sensor_id, instrument_id, native_value, native_unit, "
        f"value, unit, quality, conversion FROM {_table()} "
        "WHERE system_id = %s AND sensor_id = ANY(%s) "
        "ORDER BY sensor_id, time DESC, logger_id DESC, instrument_id DESC"
    )
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (system.id, sensors))
        cols = [column.name for column in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _demo_value(sensor_id: str, timestamp: float) -> float:
    if sensor_id == "pressure":
        return 0.8 + 0.15 * math.sin(timestamp / 80) + 0.025 * math.sin(timestamp / 7)
    return 91.0 + 3.0 * math.sin(timestamp / 300) + 0.4 * math.sin(timestamp / 19)


def _demo_latest(system: System) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    return [{
        "time": now, "sensor_id": sensor.id,
        "instrument_id": sensor.instrument_id,
        "native_value": _demo_value(sensor.id, now.timestamp()),
        "native_unit": sensor.unit, "value": _demo_value(sensor.id, now.timestamp()),
        "unit": sensor.unit, "quality": "good", "conversion": "demo-synthetic-v1",
    } for sensor in system.sensors]


def latest_data(system: System) -> Dict[str, Any]:
    rows = _demo_latest(system) if demo_enabled() else fetch_latest(system)
    by_sensor = {row["sensor_id"]: row for row in rows}
    now = datetime.now(timezone.utc)
    sensors = []
    for sensor in system.sensors:
        row = by_sensor.get(sensor.id)
        if row is None:
            sensors.append({
                "id": sensor.id, "label": sensor.label, "instrument_id": sensor.instrument_id,
                "unit": sensor.unit, "value": None, "native_value": None,
                "native_unit": None, "conversion": None, "quality": "unavailable",
                "observed_unit": None, "observed_instrument_id": None,
                "unit_compatible": None, "instrument_compatible": None, "compatible": None,
                "time": None, "age_s": None, "stale": True, "state": "unavailable",
            })
            continue
        timestamp = row["time"]
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        age = max(0.0, (now - timestamp).total_seconds())
        instrument_compatible = row["instrument_id"] == sensor.instrument_id
        unit_compatible = row["unit"] == sensor.unit
        compatible = instrument_compatible and unit_compatible
        quality = row["quality"] if compatible else "incompatible"
        value = _number(row["value"]) if compatible and quality == "good" else None
        if quality == "good" and value is None:
            quality = "invalid"
        state = ("incompatible" if not compatible else
                 "stale" if age > sensor.stale_after_s else
                 "online" if quality == "good" else quality)
        sensors.append({
            "id": sensor.id, "label": sensor.label, "instrument_id": sensor.instrument_id,
            "unit": sensor.unit, "value": value,
            "native_value": _number(row["native_value"]) if compatible else None,
            "native_unit": row["native_unit"],
            "conversion": row["conversion"], "quality": quality, "time": timestamp.isoformat(),
            "observed_unit": row["unit"], "observed_instrument_id": row["instrument_id"],
            "unit_compatible": unit_compatible, "instrument_compatible": instrument_compatible,
            "compatible": compatible,
            "age_s": round(age, 1), "stale": age > sensor.stale_after_s, "state": state,
        })
    return {"system": {"id": system.id, "label": system.label}, "sensors": sensors,
            "demo": demo_enabled()}


def _bucket_seconds(range_s: int) -> int:
    return max(1, math.ceil(range_s / _TARGET_POINTS))


def fetch_history(system: System, sensor: Sensor, range_s: int,
                  bucket_s: int) -> List[Dict[str, Any]]:
    sql = (
        f"SELECT time_bucket(make_interval(secs => %s), m.time) AS bucket, "
        "avg(m.value) FILTER (WHERE m.quality = 'good' AND m.value IS NOT NULL "
        "AND m.value > '-Infinity'::float8 AND m.value < 'Infinity'::float8 "
        "AND m.instrument_id = expected.instrument_id AND m.unit = expected.unit) AS value, "
        "count(*) AS sample_count, "
        "count(*) FILTER (WHERE m.quality = 'good' AND m.value IS NOT NULL "
        "AND m.value > '-Infinity'::float8 AND m.value < 'Infinity'::float8 "
        "AND m.instrument_id = expected.instrument_id AND m.unit = expected.unit) AS good_count, "
        "count(*) FILTER (WHERE m.quality = 'invalid') AS invalid_count, "
        "count(*) FILTER (WHERE m.quality = 'unavailable') AS unavailable_count, "
        "count(*) FILTER (WHERE m.instrument_id IS DISTINCT FROM expected.instrument_id "
        "OR m.unit IS DISTINCT FROM expected.unit) AS incompatible_count "
        f"FROM {_table()} AS m CROSS JOIN "
        "(SELECT %s::text AS instrument_id, %s::text AS unit) AS expected "
        "WHERE m.time > now() - make_interval(secs => %s) "
        "AND m.system_id = %s AND m.sensor_id = %s "
        "GROUP BY 1 ORDER BY 1 LIMIT %s"
    )
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (bucket_s, sensor.instrument_id, sensor.unit, range_s,
                          system.id, sensor.id, _MAX_HISTORY_POINTS))
        rows = cur.fetchall()
    points = [{
        "time": row[0].isoformat(), "value": _number(row[1]), "sample_count": row[2],
        "good_count": row[3], "invalid_count": row[4], "unavailable_count": row[5],
        "incompatible_count": row[6], "compatible": row[6] == 0,
    } for row in rows[:_MAX_HISTORY_POINTS]]
    return _with_gap_breaks(points, bucket_s)


def _with_gap_breaks(points: List[Dict[str, Any]], bucket_s: int) -> List[Dict[str, Any]]:
    """Insert a null chart point when one or more complete time buckets are absent."""
    separated: List[Dict[str, Any]] = []
    previous = None
    interval = timedelta(seconds=bucket_s)
    for point in points:
        current = datetime.fromisoformat(point["time"])
        if previous is not None and current - previous > interval:
            separated.append({
                "time": (previous + interval).isoformat(), "value": None,
                "sample_count": 0, "good_count": 0, "invalid_count": 0,
                "unavailable_count": 0, "incompatible_count": 0, "compatible": True,
                "gap": True,
            })
        separated.append(point)
        previous = current
    return separated[-_MAX_HISTORY_POINTS:]


def _demo_history(sensor: Sensor, range_s: int, bucket_s: int) -> List[Dict[str, Any]]:
    end = int(datetime.now(timezone.utc).timestamp()) // bucket_s * bucket_s
    start = end - range_s
    points = []
    for timestamp in range(start, end + 1, bucket_s):
        value = _demo_value(sensor.id, timestamp)
        points.append({
            "time": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "value": value, "sample_count": 1, "good_count": 1,
            "invalid_count": 0, "unavailable_count": 0, "incompatible_count": 0,
            "compatible": True,
        })
    return points[:_MAX_HISTORY_POINTS]


@router.get("/api/lab/systems")
def public_systems() -> Dict[str, Any]:
    return {
        "systems": [{"id": system.id, "label": system.label, "sensor_count": len(system.sensors)}
                    for system in CATALOG.values()],
        "demo": demo_enabled(),
    }


@router.get("/api/lab/systems/{system_id}/latest")
def system_latest(system_id: str) -> Dict[str, Any]:
    system = _system_or_404(system_id)
    try:
        return latest_data(system)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - never disclose connection details publicly
        print(f"lab database error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise HTTPException(status_code=502, detail="lab data unavailable") from exc


@router.get("/api/lab/systems/{system_id}/history")
def system_history(system_id: str, sensor_id: str = Query(...),
                   range: str = Query("1h")) -> Dict[str, Any]:  # noqa: A002
    system = _system_or_404(system_id)
    sensor = _sensor_or_404(system, sensor_id)
    if range not in _RANGES:
        raise HTTPException(status_code=400, detail=f"range must be one of {', '.join(_RANGES)}")
    range_s = _RANGES[range]
    bucket_s = _bucket_seconds(range_s)
    try:
        points = (_demo_history(sensor, range_s, bucket_s) if demo_enabled()
                  else fetch_history(system, sensor, range_s, bucket_s))
    except Exception as exc:  # noqa: BLE001 - never disclose connection details publicly
        print(f"lab database error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise HTTPException(status_code=502, detail="lab data unavailable") from exc
    return {
        "system_id": system.id, "sensor_id": sensor.id, "range": range,
        "bucket_s": bucket_s, "unit": sensor.unit, "points": points, "demo": demo_enabled(),
    }


@router.get("/lab", response_class=HTMLResponse)
def lab_index() -> str:
    return PAGE


@router.get("/lab/systems/{system_id}", response_class=HTMLResponse)
@router.get("/systems/{system_id}", response_class=HTMLResponse)
def lab_system_page(system_id: str) -> str:
    _system_or_404(system_id)
    return PAGE


@router.get("/neo/systems/{system_id}", response_class=HTMLResponse)
def neo_system_page(system_id: str) -> str:
    _system_or_404(system_id)
    return PAGE
