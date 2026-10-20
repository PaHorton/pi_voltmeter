import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

sys.path.insert(0, str(Path(__file__).parent.parent / "web"))
import app as webapp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from voltmeter.formulas import load_formulas  # noqa: E402

NOW = datetime(2026, 10, 20, 21, 0, 0, tzinfo=timezone.utc)
FORMULAS = load_formulas(Path(__file__).parent.parent / "formulas")


def row(channel, volts, formula, age=1.0, device="htvpi"):
    return {"time": NOW - timedelta(seconds=age), "device_id": device, "channel": channel,
            "volts": volts, "unit": "x", "formula": formula}


def test_payload_applies_stored_formula_to_volts():
    p = webapp.build_payload([row(0, 7.75, "pressure_torr"), row(1, 2.0, "Raw")], FORMULAS, NOW)
    ch0, ch1 = p["devices"]["htvpi"]
    assert ch0["unit"] == "Torr" and ch0["formula_applied"]
    assert ch0["value"] == pytest.approx(10 ** -0.125)
    assert ch1["value"] == 2.0 and ch1["unit"] == "V"


def test_unknown_formula_falls_back_to_volts():
    p = webapp.build_payload([row(0, 1.5, "deleted_formula")], FORMULAS, NOW)
    c = p["devices"]["htvpi"][0]
    assert c["value"] == 1.5 and c["unit"] == "V" and not c["formula_applied"]


def test_stale_null_and_nonfinite():
    p = webapp.build_payload([row(0, None, "Raw", age=120), row(1, 1e9, "pressure_torr")], FORMULAS, NOW)
    ch0, ch1 = p["devices"]["htvpi"]
    assert ch0["stale"] and ch0["value"] is None
    assert ch1["value"] is None  # 10 ** huge overflows: reported as no value, not an error


def test_endpoints(monkeypatch):
    monkeypatch.setattr(webapp, "fetch_latest", lambda: [row(1, 1.0, "Raw"), row(0, 2.0, "Raw")])
    client = TestClient(webapp.app)
    assert "Voltmeter" in client.get("/").text
    r = client.get("/api/latest")
    assert r.status_code == 200
    assert [c["channel"] for c in r.json()["devices"]["htvpi"]] == [0, 1]


def test_database_error_is_generic(monkeypatch):
    def boom():
        raise RuntimeError("password=hunter2 host=secret")
    monkeypatch.setattr(webapp, "fetch_latest", boom)
    r = TestClient(webapp.app).get("/api/latest")
    assert r.status_code == 502 and "hunter2" not in r.text


def test_table_name_validated(monkeypatch):
    monkeypatch.setenv("READINGS_TABLE", "x; drop table y")
    with pytest.raises(ValueError):
        webapp._table()
