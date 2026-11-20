import sys

import pytest

from voltmeter import lab_app


def test_simulated_voltmeter_flows_through_lab_sink_without_changing_oled_settings(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    logger = tmp_path / "logger.yaml"
    logger.write_text("""\
version: 1
logger_id: pi
instruments:
  - id: meter
    type: voltmeter
    system_id: cryo
    interval_s: 1
    sensors: {0: a, 1: b}
    options: {}
""")
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("""\
version: 1
systems:
  - id: cryo
    label: Cryo
    sensors:
      - {id: a, label: A, instrument_id: meter, unit: V, stale_after_s: 30}
      - {id: b, label: B, instrument_id: meter, unit: V, stale_after_s: 30}
""")

    class Sink:
        def __init__(self):
            self.rows = []
            self.closed = False

        def write(self, row):
            self.rows.append(row)

        def close(self):
            self.closed = True

    sink = Sink()
    monkeypatch.setattr(lab_app, "database_sink", lambda cfg: sink)
    monkeypatch.setattr(lab_app.App, "run", lambda app: app.step())
    monkeypatch.setattr(sys, "argv", ["lablogger", "--config", str(logger), "--catalog",
                                     str(catalog), "--home", str(home), "--sim"])
    lab_app.main()
    assert [row.sensor_id for row in sink.rows] == ["a", "b"]
    assert all(row.system_id == "cryo" and row.unit == "V" for row in sink.rows)
    assert sink.closed


def test_unverified_lakeshore_plugin_fails_before_database_or_serial_start(tmp_path, monkeypatch):
    logger = tmp_path / "logger.yaml"
    logger.write_text("""\
version: 1
logger_id: pi
instruments:
  - id: lake
    type: lakeshore218
    system_id: cryo
    interval_s: 5
    sensors: {1: stage}
    options: {port: /dev/ttyUSB0}
""")
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("""\
version: 1
systems:
  - id: cryo
    label: Cryo
    sensors:
      - {id: stage, label: Stage, instrument_id: lake, unit: K, stale_after_s: 30}
""")
    monkeypatch.setattr(lab_app, "database_sink", lambda cfg: (_ for _ in ()).throw(
        AssertionError("database must not be opened for unverified hardware")))
    monkeypatch.setattr(sys, "argv", ["lablogger", "--config", str(logger), "--catalog",
                                     str(catalog), "--home", str(tmp_path)])
    with pytest.raises(SystemExit, match="multi-input connection must be verified"):
        lab_app.main()


def test_empty_logger_config_fails_before_startup(tmp_path, monkeypatch):
    logger = tmp_path / "logger.yaml"
    logger.write_text("version: 1\nlogger_id: pi\ninstruments: []\n")
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("version: 1\nsystems: []\n")
    monkeypatch.setattr(lab_app, "database_sink", lambda cfg: (_ for _ in ()).throw(
        AssertionError("database must not start with an empty logger")))
    monkeypatch.setattr(sys, "argv", ["lablogger", "--config", str(logger), "--catalog",
                                     str(catalog), "--home", str(tmp_path)])
    with pytest.raises(SystemExit, match="at least one instrument"):
        lab_app.main()
