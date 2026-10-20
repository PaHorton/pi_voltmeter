"""Voltmeter web dashboard (FastAPI, deployable on Vercel).

Reads the latest rows from the TigerData ``readings`` table and applies each row's formula
(from the repo's ``formulas/`` folder, via the same parser the Pi uses) to its stored volts.

Environment:
    DATABASE_URL     postgres URL for a SELECT-only role (see db/readonly_role.sql)
    READINGS_TABLE   table name, default "readings"
    READINGS_AGG     optional continuous aggregate (db/timescale_setup.sql), used for ranges >= 6h
    STALE_AFTER_S    mark a channel stale after this many seconds, default 30
"""
from __future__ import annotations

import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
# web/ carries its own copy of voltmeter/formulas.py and formulas/ (Vercel only uploads web/).
ROOT = HERE if (HERE / "formulas").is_dir() else HERE.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from voltmeter.formulas import RAW, Formula, load_formulas  # noqa: E402

app = FastAPI(title="Voltmeter", docs_url=None, redoc_url=None)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _formulas() -> Dict[str, Formula]:
    return load_formulas(ROOT / "formulas")


def _ident(env: str, default: str) -> str:
    name = os.environ.get(env, default)
    if name and not _IDENT.match(name):
        raise ValueError(f"{env} must be a plain identifier")
    return name


def _table() -> str:
    return _ident("READINGS_TABLE", "readings")


def fetch_latest() -> List[Dict[str, Any]]:
    """Latest row per (device, channel) from the last 24 h (TimescaleDB ``last()``)."""
    import psycopg

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    sql = (f"SELECT max(time) AS time, device_id, channel, last(volts, time) AS volts, "
           f"last(unit, time) AS unit, last(formula, time) AS formula "
           f"FROM {_table()} WHERE time > now() - interval '24 hours' "
           f"GROUP BY device_id, channel ORDER BY device_id, channel")
    with psycopg.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(sql)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def build_payload(rows: List[Dict[str, Any]], formulas: Dict[str, Formula],
                  now: Optional[datetime] = None, stale_after_s: float = 30.0) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    devices: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        volts = r["volts"]
        formula = formulas.get(r["formula"] or "", None)
        if volts is None:
            value, unit, applied = None, r["unit"], False
        elif formula is not None:
            value, unit, applied = formula(volts), formula.unit, True
        else:  # formula file not on this server: fall back to volts
            value, unit, applied = volts, RAW.unit, False
        if value is not None and not math.isfinite(value):
            value = None
        age = (now - r["time"]).total_seconds()
        devices.setdefault(r["device_id"], []).append({
            "channel": r["channel"], "volts": volts, "value": value, "unit": unit,
            "formula": r["formula"], "formula_applied": applied,
            "time": r["time"].isoformat(), "age_s": round(age, 1), "stale": age > stale_after_s})
    for chans in devices.values():
        chans.sort(key=lambda c: c["channel"])
    return {"now": now.isoformat(), "devices": devices}


@app.get("/api/latest")
def latest() -> Dict[str, Any]:
    try:
        rows = fetch_latest()
    except Exception as exc:  # noqa: BLE001 - never leak connection details to the browser
        print(f"database error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise HTTPException(status_code=502, detail="database unavailable") from exc
    return build_payload(rows, _formulas(), stale_after_s=float(os.environ.get("STALE_AFTER_S", "30")))


RANGES: Dict[str, int] = {"1m": 60, "5m": 300, "10m": 600, "30m": 1800,
                          "1h": 3600, "6h": 21600, "12h": 43200, "24h": 86400}
TARGET_POINTS = 300


def bucket_seconds(range_s: int) -> int:
    return max(1, range_s // TARGET_POINTS)


AGG_MIN_RANGE_S = 6 * 3600  # shorter ranges read raw rows; longer ones use the 1-minute aggregate


def history_sql(range_s: int, agg: str = "") -> str:
    """Bucketed average volts (per formula name, so a formula change mid-range stays correct)."""
    if agg and range_s >= AGG_MIN_RANGE_S:
        # re-average the 1-minute buckets, weighted by their row counts
        return (f"SELECT time_bucket(make_interval(secs => %s), bucket) AS t, formula, "
                f"sum(volts * n) / sum(n) AS volts FROM {agg} "
                f"WHERE bucket > now() - make_interval(secs => %s) AND device_id = %s AND channel = %s "
                f"GROUP BY 1, 2 ORDER BY 1")
    return (f"SELECT time_bucket(make_interval(secs => %s), time) AS t, formula, avg(volts) AS volts "
            f"FROM {_table()} "
            f"WHERE time > now() - make_interval(secs => %s) AND device_id = %s AND channel = %s "
            f"GROUP BY 1, 2 ORDER BY 1")


def fetch_history(range_s: int, bucket_s: int, device: str, channel: int) -> List[Dict[str, Any]]:
    import psycopg

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    sql = history_sql(range_s, _ident("READINGS_AGG", ""))
    with psycopg.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(sql, (bucket_s, range_s, device, channel))
        return [{"time": t, "formula": f, "volts": v} for t, f, v in cur.fetchall()]


def build_history(rows: List[Dict[str, Any]], formulas: Dict[str, Formula],
                  bucket_s: Optional[int] = None) -> Dict[str, Any]:
    """Points are [epoch_ms, volts, value]. With bucket_s, a gap longer than 3 buckets (the Pi was
    offline) gets a null point so the chart breaks the line instead of joining across it."""
    points: List[List[Any]] = []
    unit, formula_name = "V", None
    for r in rows:
        ms = int(r["time"].timestamp() * 1000)
        if bucket_s and points and ms - points[-1][0] > 3 * bucket_s * 1000:
            points.append([points[-1][0] + bucket_s * 1000, None, None])
        volts = r["volts"]
        formula = formulas.get(r["formula"] or "")
        if volts is None:
            value = None
        else:
            value = formula(volts) if formula is not None else volts
            if not math.isfinite(value):
                value = None
        if formula is not None:
            unit, formula_name = formula.unit, r["formula"]
        points.append([ms, volts, value])
    return {"unit": unit, "formula": formula_name, "points": points}


@app.get("/api/history")
def history(device: str = Query(..., max_length=100), channel: int = Query(..., ge=0, le=3),
            range: str = Query("10m")) -> Dict[str, Any]:  # noqa: A002 - public query parameter name
    if range not in RANGES:
        raise HTTPException(status_code=400, detail=f"range must be one of {', '.join(RANGES)}")
    range_s = RANGES[range]
    bucket_s = bucket_seconds(range_s)
    try:
        rows = fetch_history(range_s, bucket_s, device, channel)
    except Exception as exc:  # noqa: BLE001
        print(f"database error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise HTTPException(status_code=502, detail="database unavailable") from exc
    out = build_history(rows, _formulas(), bucket_s)
    return {"range": range, "bucket_s": bucket_s, "device": device, "channel": channel, **out}


_PAGE_FILE = Path(__file__).parent / "index.html"


THEMES = ("default", "neon", "neo", "nge")


def _page(theme: str) -> str:
    return _PAGE_FILE.read_text(encoding="utf-8").replace('<body data-theme="default"', f'<body data-theme="{theme}"', 1)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _page("default")


@app.get("/neon", response_class=HTMLResponse)
def neon() -> str:
    return _page("neon")


@app.get("/neo", response_class=HTMLResponse)
def neo() -> str:
    return _page("neo")


@app.get("/nge", response_class=HTMLResponse)
def nge() -> str:
    return _page("nge")
