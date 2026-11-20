from datetime import datetime, timezone

import pytest

from voltmeter.measurements import Measurement


def measurement(**overrides):
    values = dict(time=datetime.now(timezone.utc), logger_id="logger-1",
                  system_id="cryo", instrument_id="meter", sensor_id="pressure",
                  native_value=5.0, native_unit="V", value=0.1, unit="Torr")
    values.update(overrides)
    return Measurement(**values)


def test_measurement_requires_stable_safe_ids_and_aware_time():
    with pytest.raises(ValueError, match="sensor_id"):
        measurement(sensor_id="../pressure")
    with pytest.raises(ValueError, match="timezone"):
        measurement(time=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone"):
        measurement(time=None)


def test_measurement_quality_and_nonfinite_values():
    assert measurement(value=None, quality="invalid").quality == "invalid"
    with pytest.raises(ValueError, match="finite"):
        measurement(value=float("nan"))
    with pytest.raises(ValueError, match="require a value"):
        measurement(value=None)
    with pytest.raises(ValueError, match="cannot have a value"):
        measurement(quality="unavailable")
    with pytest.raises(ValueError, match="unit"):
        measurement(unit=" ")
    with pytest.raises(ValueError, match="numeric"):
        measurement(value=True)
