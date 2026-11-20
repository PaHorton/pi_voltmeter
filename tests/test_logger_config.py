import pytest

from voltmeter.catalog import Sensor, System
from voltmeter.logger_config import load_logger_config, validate_catalog


def test_load_logger_config(tmp_path):
    path = tmp_path / "logger.yaml"
    path.write_text("""\
version: 1
logger_id: pi-1
instruments:
  - id: adc
    type: voltmeter
    system_id: cryo
    interval_s: 2
    sensors: {0: pressure, 1: temperature}
    options: {}
""")
    config = load_logger_config(path)
    assert config.instruments[0].sensors == {0: "pressure", 1: "temperature"}
    assert config.logger_id == "pi-1"
    system = System("cryo", "Cryocooler",
                    (Sensor("pressure", "Pressure", "adc", "Torr", 30),
                     Sensor("temperature", "Temperature", "adc", "K", 30)))
    validate_catalog(config, {"cryo": system})
    with pytest.raises(ValueError, match="unknown system"):
        validate_catalog(config, {})
    with pytest.raises(ValueError, match="not registered"):
        validate_catalog(config, {"cryo": System("cryo", "Cryocooler", ())})


def test_reject_duplicate_sensor_ids(tmp_path):
    path = tmp_path / "logger.yaml"
    path.write_text("""\
version: 1
logger_id: pi
instruments:
  - id: adc
    type: voltmeter
    system_id: cryo
    interval_s: 1
    sensors: {0: same, 1: same}
    options: {}
""")
    with pytest.raises(ValueError, match="duplicate sensor"):
        load_logger_config(path)
