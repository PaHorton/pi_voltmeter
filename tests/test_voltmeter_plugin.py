import math

from voltmeter.formulas import Formula, RAW
from voltmeter.settings import Settings
from voltmeter.voltmeter_plugin import VoltmeterPlugin


class Adc:
    def read_volts(self, channel, n, range_v):
        return (1.0, -1.0)[channel]

    def close(self):
        pass


def test_voltmeter_plugin_emits_native_values_and_conversion_provenance():
    settings = Settings(formulas=["scale", "sqrt"])
    formulas = {"Raw": RAW, "scale": Formula.parse("scale", "mV\nV * 1000"),
                "sqrt": Formula.parse("sqrt", "V\nsqrt(V)")}
    plugin = VoltmeterPlugin(Adc(), settings, [2, 1], lambda: formulas,
                             "pi", "cryo", "adc", ["pressure", "temperature"])
    measurements = plugin.sample()
    assert measurements[0].native_value == 2
    assert measurements[0].value == 2000
    assert measurements[0].conversion == "scale: V * 1000"
    assert measurements[1].quality == "invalid"
    assert measurements[1].value is None
    assert math.isfinite(measurements[1].native_value)
