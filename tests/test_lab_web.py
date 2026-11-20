import sys
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
sys.path.insert(0, str(WEB))
import app as legacy_web  # noqa: E402
import lab_dashboard as lab  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(legacy_web.app)


def test_opt_in_lab_landing_and_direct_system_url(monkeypatch):
    monkeypatch.setenv("LAB_MODE", "1")
    assert "Public Lab Systems" in client.get("/").text
    assert client.get("/systems/demo-lab").status_code == 200
    assert client.get("/systems/unknown").status_code == 404


class FakeCursor:
    def __init__(self, rows, columns):
        self.rows = rows
        self.description = [SimpleNamespace(name=name) for name in columns]
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.fake_cursor


def test_catalog_is_explicit_public_structure_and_copy_matches_source():
    source = ROOT / "voltmeter"
    for name in ("catalog.py", "measurements.py"):
        assert (WEB / "voltmeter" / name).read_bytes() == (source / name).read_bytes()
    systems = lab.public_systems()["systems"]
    assert systems == [{"id": "demo-lab", "label": "Demonstration lab", "sensor_count": 2}]
    assert [sensor.id for sensor in lab.CATALOG["demo-lab"].sensors] == ["pressure", "temperature"]


def test_live_catalog_is_opt_in_and_matches_logger_example(monkeypatch):
    monkeypatch.delenv("LAB_CATALOG", raising=False)
    assert set(lab._load_public_catalog()) == {"demo-lab"}
    monkeypatch.setenv("LAB_CATALOG", "catalog-live.yaml")
    live = lab._load_public_catalog()
    assert live == lab.load_catalog(ROOT / "examples" / "lab-catalog.yaml")
    assert set(live) == {"cryocooler"}
    assert [(sensor.id, sensor.instrument_id, sensor.unit) for sensor in live["cryocooler"].sensors] == [
        ("pressure", "voltmeter", "Torr"),
        ("voltage", "voltmeter", "V"),
        ("temperature-1", "lakeshore", "K"),
        ("temperature-2", "lakeshore", "K"),
        ("temperature-3", "lakeshore", "K"),
    ]
    monkeypatch.setenv("LAB_CATALOG", "missing.yaml")
    with pytest.raises(FileNotFoundError):
        lab._load_public_catalog()


def test_live_catalog_routes_report_matching_and_mismatched_measurements(monkeypatch):
    monkeypatch.setenv("LAB_CATALOG", "catalog-live.yaml")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setattr(lab, "CATALOG", lab._load_public_catalog())
    now = datetime.now(timezone.utc)
    columns = ["time", "sensor_id", "instrument_id", "native_value", "native_unit",
               "value", "unit", "quality", "conversion"]
    rows = [
        (now, "pressure", "voltmeter", 1.0, "V", 1.0, "V", "good", "Raw"),
        (now, "temperature-1", "lakeshore", 4.2, "K", 4.2, "K", "good", None),
    ]
    monkeypatch.setattr(lab, "_connect", lambda: FakeConnection(FakeCursor(rows, columns)))

    systems = client.get("/api/lab/systems").json()
    latest = client.get("/api/lab/systems/cryocooler/latest").json()
    assert systems["systems"] == [{"id": "cryocooler", "label": "Cryocooler", "sensor_count": 5}]
    sensors = {sensor["id"]: sensor for sensor in latest["sensors"]}
    assert sensors["pressure"]["state"] == "incompatible"
    assert sensors["pressure"]["value"] is None
    assert sensors["temperature-1"]["state"] == "online"
    assert sensors["temperature-1"]["value"] == 4.2
    assert sensors["temperature-2"]["state"] == "unavailable"


def test_web_measurement_copy_has_exact_measurement_fields():
    namespace = {}
    source = (WEB / "voltmeter" / "measurements.py").read_text(encoding="utf-8")
    exec(compile(source, "web/voltmeter/measurements.py", "exec"), namespace)
    assert [field.name for field in fields(namespace["Measurement"])] == [
        "time", "logger_id", "system_id", "instrument_id", "sensor_id",
        "native_value", "native_unit", "value", "unit", "quality", "conversion",
    ]
    schema = (ROOT / "db" / "lab_measurements.sql").read_text(encoding="utf-8")
    table = schema.split("CREATE TABLE IF NOT EXISTS public.lab_measurements (", 1)[1].split(");", 1)[0]
    columns = [line.strip().split()[0] for line in table.splitlines()
               if line.strip() and not line.strip().startswith(("CHECK", "PRIMARY KEY", "OR", "--"))]
    assert columns == [field.name for field in fields(namespace["Measurement"])]


def test_latest_query_is_select_only_and_scoped_with_parameters(monkeypatch):
    now = datetime.now(timezone.utc)
    columns = ["time", "sensor_id", "instrument_id", "native_value", "native_unit",
               "value", "unit", "quality", "conversion"]
    rows = [
        (now - timedelta(seconds=31), "pressure", "demo-gauge", 0.4, "V", 0.2, "Torr", "good", "cal-v1"),
        (now, "temperature", "demo-thermometer", None, "V", None, "°C", "invalid", "cal-v1"),
    ]
    cursor = FakeCursor(rows, columns)
    monkeypatch.setattr(lab, "_connect", lambda: FakeConnection(cursor))

    result = lab.latest_data(lab.CATALOG["demo-lab"])
    assert cursor.sql.lstrip().startswith("SELECT")
    assert "instrument_id" in cursor.sql.split("FROM", 1)[0]
    assert "FROM public.lab_measurements" in cursor.sql
    assert "system_id = %s AND sensor_id = ANY(%s)" in cursor.sql
    assert cursor.params == ("demo-lab", ["pressure", "temperature"])
    pressure, temperature = result["sensors"]
    assert pressure["state"] == "stale" and pressure["value"] == pytest.approx(0.2)
    assert temperature["quality"] == "invalid" and temperature["value"] is None


@pytest.mark.parametrize(
    ("instrument_id", "unit", "instrument_compatible", "unit_compatible"),
    [
        ("demo-gauge", "Pa", True, False),
        ("another-gauge", "Torr", False, True),
    ],
)
def test_latest_flags_mismatched_unit_or_instrument(
    monkeypatch, instrument_id, unit, instrument_compatible, unit_compatible
):
    now = datetime.now(timezone.utc)
    columns = ["time", "sensor_id", "instrument_id", "native_value", "native_unit",
               "value", "unit", "quality", "conversion"]
    rows = [(now, "pressure", instrument_id, 0.4, "V", 12.0, unit, "good", "cal-v1")]
    monkeypatch.setattr(lab, "_connect", lambda: FakeConnection(FakeCursor(rows, columns)))

    pressure = lab.latest_data(lab.CATALOG["demo-lab"])["sensors"][0]
    assert pressure["value"] is None
    assert pressure["state"] == pressure["quality"] == "incompatible"
    assert pressure["compatible"] is False
    assert pressure["instrument_compatible"] is instrument_compatible
    assert pressure["unit_compatible"] is unit_compatible
    assert pressure["observed_unit"] == unit
    assert pressure["observed_instrument_id"] == instrument_id


def test_latest_missing_or_bad_values_never_become_zero(monkeypatch):
    now = datetime.now(timezone.utc)
    columns = ["time", "sensor_id", "instrument_id", "native_value", "native_unit",
               "value", "unit", "quality", "conversion"]
    rows = [(now, "pressure", "demo-gauge", 2.0, "V", float("nan"), "Torr", "good", None)]
    monkeypatch.setattr(lab, "_connect", lambda: FakeConnection(FakeCursor(rows, columns)))
    output = lab.latest_data(lab.CATALOG["demo-lab"])["sensors"]
    assert output[0]["value"] is None and output[0]["quality"] == "invalid"
    assert output[1]["value"] is None and output[1]["state"] == "unavailable"
    assert output[1]["stale"] is True


def test_history_averages_engineering_values_and_bounds_select(monkeypatch):
    timestamp = datetime(2026, 11, 20, tzinfo=timezone.utc)
    cursor = FakeCursor([(timestamp, 4.25, 3, 2, 1, 0, 1)], [])
    monkeypatch.setattr(lab, "_connect", lambda: FakeConnection(cursor))

    result = lab.fetch_history(lab.CATALOG["demo-lab"], lab.CATALOG["demo-lab"].sensors[0], 3600, 12)
    assert cursor.sql.lstrip().startswith("SELECT")
    assert "m.instrument_id = expected.instrument_id AND m.unit = expected.unit" in cursor.sql
    assert "count(*) FILTER (WHERE m.instrument_id IS DISTINCT FROM expected.instrument_id " in cursor.sql
    assert "avg(m.native_value)" not in cursor.sql
    assert "FROM public.lab_measurements" in cursor.sql
    assert "LIMIT %s" in cursor.sql
    assert cursor.params == (12, "demo-gauge", "Torr", 3600, "demo-lab", "pressure",
                             lab._MAX_HISTORY_POINTS)
    assert result[0]["value"] == 4.25
    assert result[0]["invalid_count"] == 1
    assert result[0]["incompatible_count"] == 1 and result[0]["compatible"] is False


def test_history_inserts_break_point_when_offline_buckets_are_missing(monkeypatch):
    first = datetime(2026, 11, 20, tzinfo=timezone.utc)
    rows = [
        (first, 1.0, 1, 1, 0, 0, 0),
        (first + timedelta(seconds=30), 2.0, 1, 1, 0, 0, 0),
    ]
    monkeypatch.setattr(lab, "_connect", lambda: FakeConnection(FakeCursor(rows, [])))
    separated = lab.fetch_history(
        lab.CATALOG["demo-lab"], lab.CATALOG["demo-lab"].sensors[0], 3600, bucket_s=10
    )
    assert len(separated) == 3
    assert separated[1]["value"] is None and separated[1]["gap"] is True
    assert separated[1]["time"] == (first + timedelta(seconds=10)).isoformat()
    assert "if (!Number.isFinite(row.value) || !Number.isFinite(t)) { flush(); continue; }" in lab.PAGE


def test_table_identifier_rejects_sql_fragments(monkeypatch):
    monkeypatch.setenv("LAB_MEASUREMENTS_TABLE", "public.lab_measurements; DROP TABLE x")
    with pytest.raises(ValueError):
        lab._table()
    monkeypatch.setenv("LAB_MEASUREMENTS_TABLE", "safe_name")
    assert lab._table() == "safe_name"


def test_lab_routes_scope_ids_and_keep_legacy_dashboard_unchanged(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert client.get("/").status_code == 200
    assert 'data-theme="default"' in client.get("/").text
    assert 'data-theme="neon"' in client.get("/neon").text
    assert client.get("/lab").status_code == 200
    assert client.get("/lab/systems/demo-lab").status_code == 200
    assert client.get("/lab/systems/not-published").status_code == 404
    assert client.get("/api/lab/systems/not-published/latest").status_code == 404
    assert client.get(
        "/api/lab/systems/demo-lab/history",
        params={"sensor_id": "not-published", "range": "1h"},
    ).status_code == 404
    assert client.get(
        "/api/lab/systems/demo-lab/history",
        params={"sensor_id": "x" * 100, "range": "1h"},
    ).status_code == 404


def test_demo_mode_uses_two_synthetic_sensors_without_database(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def no_connection():
        raise AssertionError("demo mode must not open a database connection")

    monkeypatch.setattr(lab, "_connect", no_connection)
    systems = client.get("/api/lab/systems")
    latest = client.get("/api/lab/systems/demo-lab/latest")
    history = client.get(
        "/api/lab/systems/demo-lab/history",
        params={"sensor_id": "pressure", "range": "1h"},
    )
    assert systems.status_code == latest.status_code == history.status_code == 200
    assert systems.json()["demo"] is True
    sensors = latest.json()["sensors"]
    assert [sensor["id"] for sensor in sensors] == ["pressure", "temperature"]
    assert all(sensor["value"] is not None and sensor["state"] == "online" for sensor in sensors)
    points = history.json()["points"]
    assert 1 < len(points) <= lab._MAX_HISTORY_POINTS
    assert all(point["value"] is not None and point["good_count"] == 1 for point in points)
    assert set(lab._RANGES) >= {"1m", "5m", "10m", "30m"}
    for range_name in ("1m", "5m", "10m", "30m"):
        response = client.get(
            "/api/lab/systems/demo-lab/history",
            params={"sensor_id": "pressure", "range": range_name},
        )
        assert response.status_code == 200 and response.json()["range"] == range_name


def test_lab_database_errors_are_generic(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setattr(lab, "fetch_latest", lambda system: (_ for _ in ()).throw(
        RuntimeError("password=secret")))
    response = client.get("/api/lab/systems/demo-lab/latest")
    assert response.status_code == 502
    assert "secret" not in response.text
