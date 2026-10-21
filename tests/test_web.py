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
    assert "VOLTMETER" in client.get("/").text
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


def hrow(offset_s, volts, formula="pressure_torr"):
    return {"time": NOW - timedelta(seconds=offset_s), "formula": formula, "volts": volts}


def test_bucket_sizes_target_about_300_points():
    assert webapp.bucket_seconds(60) == 1
    assert webapp.bucket_seconds(3600) == 12
    assert webapp.bucket_seconds(86400) == 288
    assert set(webapp.RANGES) == {"1m", "5m", "10m", "30m", "1h", "6h", "12h", "24h"}


def test_history_payload_applies_formula_and_handles_gaps():
    out = webapp.build_history([hrow(20, 7.75), hrow(10, None), hrow(0, 1e9)], FORMULAS)
    assert out["unit"] == "Torr" and out["formula"] == "pressure_torr"
    (t0, v0, y0), (_, v1, y1), (_, _, y2) = out["points"]
    assert v0 == 7.75 and y0 == pytest.approx(10 ** -0.125)
    assert v1 is None and y1 is None and y2 is None
    assert t0 == int((NOW - timedelta(seconds=20)).timestamp() * 1000)


def test_history_endpoint(monkeypatch):
    seen = {}

    def fake(range_s, bucket_s, device, channel):
        seen.update(range_s=range_s, bucket_s=bucket_s, device=device, channel=channel)
        return [hrow(1, 2.0, "Raw")]
    monkeypatch.setattr(webapp, "fetch_history", fake)
    client = TestClient(webapp.app)
    r = client.get("/api/history", params={"device": "htvpi", "channel": 1, "range": "1h"})
    assert r.status_code == 200 and r.json()["bucket_s"] == 12 and len(r.json()["points"]) == 1
    assert seen == {"range_s": 3600, "bucket_s": 12, "device": "htvpi", "channel": 1}
    assert client.get("/api/history", params={"device": "x", "channel": 0, "range": "2d"}).status_code == 400
    assert client.get("/api/history", params={"device": "x", "channel": 9}).status_code == 422
    assert client.get("/api/history").status_code == 422


def test_history_database_error_is_generic(monkeypatch):
    def boom(*a):
        raise RuntimeError("password=hunter2")
    monkeypatch.setattr(webapp, "fetch_history", boom)
    r = TestClient(webapp.app).get("/api/history", params={"device": "d", "channel": 0})
    assert r.status_code == 502 and "hunter2" not in r.text


def test_history_breaks_line_across_offline_gap():
    rows = [hrow(1000, 1.0, "Raw"), hrow(990, 1.0, "Raw"), hrow(100, 2.0, "Raw"), hrow(90, 2.0, "Raw")]
    pts = webapp.build_history(rows, FORMULAS, bucket_s=10)["points"]
    assert len(pts) == 5
    assert pts[2][1] is None and pts[2][2] is None  # null point between the two clusters
    assert [p[1] for p in pts if p[1] is not None] == [1.0, 1.0, 2.0, 2.0]
    assert len(webapp.build_history(rows, FORMULAS)["points"]) == 4  # no bucket size: unchanged


def test_history_sql_uses_aggregate_only_for_long_ranges():
    short = webapp.history_sql(3600, "readings_1m")
    long = webapp.history_sql(86400, "readings_1m")
    assert "FROM readings " in short and "readings_1m" not in short
    assert "FROM readings_1m" in long and "sum(volts * n) / sum(n)" in long
    assert "readings_1m" not in webapp.history_sql(86400, "")
    assert "time_bucket" in short and "last(" in webapp.fetch_latest.__code__.co_consts.__str__()


def test_agg_name_validated(monkeypatch):
    monkeypatch.setenv("READINGS_AGG", "x; drop table y")
    with pytest.raises(ValueError):
        webapp._ident("READINGS_AGG", "")


def test_web_copies_match_sources():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    pairs = [(root / "voltmeter/formulas.py", root / "web/voltmeter/formulas.py")]
    pairs += [(f, root / "web/formulas" / f.name) for f in (root / "formulas").glob("*.txt")]
    for src, copy in pairs:
        assert copy.read_bytes() == src.read_bytes(), f"{copy} out of sync; copy it from {src}"


def test_theme_routes():
    client = TestClient(webapp.app)
    for path, theme in [("/", "default"), ("/neon", "neon"), ("/neo", "neo"), ("/nge", "nge"), ("/98", "98")]:
        r = client.get(path)
        assert r.status_code == 200
        assert f'<body data-theme="{theme}"' in r.text


def test_98css_only_on_98_route():
    client = TestClient(webapp.app)
    assert "unpkg.com/98.css" in client.get("/98").text
    assert "unpkg.com/98.css" not in client.get("/").text
