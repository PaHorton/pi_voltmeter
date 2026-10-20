"""Voltmeter web dashboard (FastAPI, deployable on Vercel).

Reads the latest rows from the TigerData ``readings`` table and applies each row's formula
(from the repo's ``formulas/`` folder, via the same parser the Pi uses) to its stored volts.

Environment:
    DATABASE_URL     postgres URL for a SELECT-only role (see db/readonly_role.sql)
    READINGS_TABLE   table name, default "readings"
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from voltmeter.formulas import RAW, Formula, load_formulas  # noqa: E402

app = FastAPI(title="Voltmeter", docs_url=None, redoc_url=None)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _formulas() -> Dict[str, Formula]:
    return load_formulas(ROOT / "formulas")


def _table() -> str:
    table = os.environ.get("READINGS_TABLE", "readings")
    if not _IDENT.match(table):
        raise ValueError("READINGS_TABLE must be a plain identifier")
    return table


def fetch_latest() -> List[Dict[str, Any]]:
    """Latest row per (device, channel) from the last 24 h."""
    import psycopg

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    sql = (f"SELECT DISTINCT ON (device_id, channel) time, device_id, channel, volts, unit, formula "
           f"FROM {_table()} WHERE time > now() - interval '24 hours' "
           f"ORDER BY device_id, channel, time DESC")
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


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voltmeter</title>
<style>
 body{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:1.5rem}
 h1{font-size:1.1rem;font-weight:500;color:#aaa;margin:0 0 1rem}
 .dev{margin-bottom:2rem}.dev h2{font-size:1rem;color:#8ab4f8;margin:0 0 .5rem}
 .row{display:flex;gap:1rem;flex-wrap:wrap}
 .card{background:#1c1c1e;border-radius:12px;padding:1rem 1.25rem;min-width:200px}
 .card.stale{opacity:.45}
 .val{font-size:2.2rem;font-variant-numeric:tabular-nums}.unit{font-size:1rem;color:#aaa;margin-left:.3rem}
 .meta{font-size:.8rem;color:#888;margin-top:.4rem}
 #status{font-size:.8rem;color:#888;margin-top:1rem}
</style></head><body>
<h1>Voltmeter</h1><div id="out">Loading…</div><div id="status"></div>
<script>
function fmt(v){if(v===null)return"ERR";const a=Math.abs(v);
 return(a!==0&&(a<0.001||a>=1e6))?v.toExponential(3):v.toPrecision(5).replace(/\\.?0+$/,"")}
async function tick(){
 const s=document.getElementById("status");
 try{
  const r=await fetch("/api/latest",{cache:"no-store"});
  if(!r.ok)throw new Error(r.status);
  const d=await r.json();const out=document.getElementById("out");out.innerHTML="";
  const names=Object.keys(d.devices);
  if(!names.length)out.textContent="No readings in the last 24 hours.";
  for(const n of names){
   const dev=document.createElement("div");dev.className="dev";
   dev.innerHTML="<h2></h2><div class=row></div>";dev.querySelector("h2").textContent=n;
   for(const c of d.devices[n]){
    const card=document.createElement("div");card.className="card"+(c.stale?" stale":"");
    card.innerHTML="<div>Ch <span class=ch></span></div><div><span class=val></span><span class=unit></span></div><div class=meta></div>";
    card.querySelector(".ch").textContent=c.channel;
    card.querySelector(".val").textContent=fmt(c.value);
    card.querySelector(".unit").textContent=c.unit||"";
    card.querySelector(".meta").textContent=(c.formula||"")+" · "+(c.volts===null?"–":c.volts.toFixed(3)+" V")+" · "+(c.stale?"stale, ":"")+Math.round(c.age_s)+"s ago";
    dev.querySelector(".row").appendChild(card);
   }
   out.appendChild(dev);
  }
  s.textContent="Updated "+new Date().toLocaleTimeString();
 }catch(e){s.textContent="Update failed ("+e.message+"); retrying…"}
}
tick();setInterval(tick,2000);
</script></body></html>
"""
