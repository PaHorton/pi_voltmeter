from types import SimpleNamespace

import pytest

from voltmeter.lakeshore_plugin import LakeShorePlugin


class Driver:
    def __init__(self):
        self.queried = []
        self.closed = False

    def read_kelvin(self, channel):
        self.queried.append(channel)
        return SimpleNamespace(kelvin=4.2 if channel == 1 else None,
                               valid=channel == 1, error="open sensor" if channel != 1 else None)

    def close(self):
        self.closed = True


def test_multiple_channels_share_connection_and_invalid_readings_are_explicit():
    driver = Driver()
    plugin = LakeShorePlugin(lambda: driver, "pi", "cryo", "lakeshore",
                             {1: "cold", 2: "warm"})
    plugin.open()
    samples = plugin.sample()
    plugin.close()
    assert driver.queried == [1, 2]
    assert samples[0].value == 4.2 and samples[0].unit == "K"
    assert samples[1].value is None and samples[1].quality == "invalid"
    assert driver.closed


def test_invalid_channel_or_unopened_plugin():
    with pytest.raises(ValueError):
        LakeShorePlugin(Driver, "pi", "cryo", "lake", {9: "bad"})
    with pytest.raises(RuntimeError, match="not open"):
        LakeShorePlugin(Driver, "pi", "cryo", "lake", {1: "ok"}).sample()
