import sys
import threading
from types import SimpleNamespace

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


def test_voltmeter_and_three_lakeshore_inputs_publish_together(tmp_path, monkeypatch):
    logger = tmp_path / "logger.yaml"
    logger.write_text("""\
version: 1
logger_id: pi
instruments:
  - id: meter
    type: voltmeter
    system_id: cryo
    interval_s: 1
    sensors: {0: pressure, 1: voltage}
    options: {}
  - id: lake
    type: lakeshore218
    system_id: cryo
    interval_s: 5
    sensors: {1: stage1, 2: stage2, 3: stage3}
    options: {port: /dev/ttyUSB0, baudrate: 9600, bytesize: 7, parity: O, stopbits: 1, timeout: 1.0}
""")
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("""\
version: 1
systems:
  - id: cryo
    label: Cryo
    sensors:
      - {id: pressure, label: Pressure, instrument_id: meter, unit: V, stale_after_s: 30}
      - {id: voltage, label: Voltage, instrument_id: meter, unit: V, stale_after_s: 30}
      - {id: stage1, label: Stage 1, instrument_id: lake, unit: K, stale_after_s: 30}
      - {id: stage2, label: Stage 2, instrument_id: lake, unit: K, stale_after_s: 30}
      - {id: stage3, label: Stage 3, instrument_id: lake, unit: K, stale_after_s: 30}
""")
    ready = threading.Event()
    rows = []
    drivers = []

    class Sink:
        def write(self, row):
            rows.append(row)
            if row.sensor_id == "stage3":
                ready.set()

        def close(self):
            pass

    class Driver:
        def __init__(self, port, settings):
            self.port = port
            self.channels = []
            self.closed = False
            drivers.append(self)

        def read_kelvin(self, channel):
            self.channels.append(channel)
            return SimpleNamespace(kelvin=channel + 4.0, valid=True, error=None)

        def close(self):
            self.closed = True

    def run(app):
        app.step()
        assert ready.wait(2)

    monkeypatch.setattr(lab_app, "LakeShore218", Driver)
    monkeypatch.setattr(lab_app, "database_sink", lambda cfg: Sink())
    monkeypatch.setattr(lab_app.App, "run", run)
    monkeypatch.setattr(sys, "argv", ["lablogger", "--config", str(logger), "--catalog",
                                     str(catalog), "--home", str(tmp_path), "--sim"])
    lab_app.main()
    assert {row.sensor_id for row in rows} == {"pressure", "voltage", "stage1", "stage2", "stage3"}
    assert [row.value for row in rows if row.instrument_id == "lake"] == [5.0, 6.0, 7.0]
    assert len(drivers) == 1 and drivers[0].channels == [1, 2, 3] and drivers[0].closed


def test_invalid_lakeshore_options_fail_before_database_start(tmp_path, monkeypatch):
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
        AssertionError("database must not start with invalid options")))
    monkeypatch.setattr(sys, "argv", ["lablogger", "--config", str(logger), "--catalog",
                                     str(catalog), "--home", str(tmp_path)])
    with pytest.raises(SystemExit, match="Lake Shore options require"):
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
