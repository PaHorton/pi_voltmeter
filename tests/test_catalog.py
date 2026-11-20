import pytest

from voltmeter.catalog import load_catalog


def test_catalog_loads_public_system_and_sensor(tmp_path):
    path = tmp_path / "catalog.yaml"
    path.write_text("""\
version: 1
systems:
  - id: cryo
    label: Cryocooler
    sensors:
      - id: pressure
        label: Pressure
        instrument_id: voltmeter
        unit: Torr
        stale_after_s: 30
""")
    system = load_catalog(path)["cryo"]
    assert system.sensors[0].instrument_id == "voltmeter"
    assert system.sensors[0].stale_after_s == 30


@pytest.mark.parametrize("bad", [
    "version: 2\nsystems: []",
    "version: 1\nsystems:\n  - id: bad/path\n    label: x\n    sensors: []",
    "version: 1\nsystems:\n  - id: ok\n    label: x\n    sensors: []\n    password: secret",
])
def test_catalog_rejects_invalid_or_private_fields(tmp_path, bad):
    path = tmp_path / "catalog.yaml"
    path.write_text(bad)
    with pytest.raises(ValueError):
        load_catalog(path)
