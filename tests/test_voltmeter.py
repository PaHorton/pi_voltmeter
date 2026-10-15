import csv
from datetime import datetime

import pytest

from voltmeter.app import App, NullPad, SimDisplay
from pathlib import Path

from voltmeter.config import Config
from voltmeter.formulas import Formula, FormulaError, load_formulas
from voltmeter.settings import Settings
from voltmeter.sinks import ChannelReading, CsvSink, Reading
from voltmeter.ui import Button
from voltmeter.usb import find_usb_mount, import_formulas


def test_formula_eval():
    f = Formula.parse("t", "degC\nV * 100 + sqrt(4)")
    assert f.unit == "degC"
    assert f(0.25) == pytest.approx(27.0)


@pytest.mark.parametrize("expr", ["__import__('os').system('x')", "V.real", "[V]", "open('f')", "V if V else 1"])
def test_formula_rejects_unsafe(expr):
    with pytest.raises(FormulaError):
        Formula.parse("bad", f"u\n{expr}")


def test_formula_math_error_is_nan():
    assert str(Formula.parse("l", "u\nlog(V)")(-1)) == "nan"
    assert str(Formula.parse("p", "u\n9**9**9")(0)) == "nan"


def test_load_formulas_skips_bad(tmp_path):
    (tmp_path / "ok.txt").write_text("mV\nV*1000\n")
    (tmp_path / "bad.txt").write_text("only one line")
    assert sorted(load_formulas(tmp_path)) == ["Raw", "ok"]


def test_settings_roundtrip_and_sanitize(tmp_path):
    p = tmp_path / "s.json"
    s = Settings(formulas=["a", "b"], n=30)
    s.save(p)
    assert Settings.load(p).n == 30
    p.write_text('{"n": 7, "bogus": 1}')
    assert Settings.load(p).n == 1
    p.write_text("not json")
    assert Settings.load(p) == Settings()


def test_find_usb_mount(tmp_path):
    m = tmp_path / "mounts"
    m.write_text("/dev/mmcblk0p2 / ext4 rw 0 0\n/dev/sda1 /media/pi/MY\\040USB vfat rw 0 0\n")
    assert str(find_usb_mount(mounts_file=str(m))) == "/media/pi/MY USB"
    m.write_text("/dev/mmcblk0p2 / ext4 rw 0 0\n")
    assert find_usb_mount(mounts_file=str(m)) is None


def test_import_formulas(tmp_path):
    usb, dest = tmp_path / "usb", tmp_path / "dest"
    (usb / "formulas").mkdir(parents=True)
    (usb / "formulas" / "a.txt").write_text("V\nV\n")
    (usb / "formulas" / "bad.txt").write_text("x")
    (usb / "formulas" / "._a.txt").write_bytes(b"\x00\x05")
    assert import_formulas(usb, dest) == 1
    assert [p.name for p in dest.iterdir()] == ["a.txt"]


def test_csv_sink(tmp_path):
    sink = CsvSink(tmp_path / "logs", 2)
    ch = ChannelReading(1.5, 150.0, "degC", "lm35")
    sink.write(Reading(datetime.now(), [ch, ch]))
    sink.close()
    rows = list(csv.reader(open(sink.path)))
    assert rows[0][:3] == ["timestamp", "ch0_volts", "ch0_value"]
    assert rows[1][1:5] == ["1.500000", "150", "degC", "lm35"]


class FixedAdc:
    def read_volts(self, channel, n, range_v):
        return 1.0 + channel

    def close(self):
        pass


def test_app_end_to_end(tmp_path):
    mount = tmp_path / "usbstick"
    (mount / "formulas").mkdir(parents=True)
    (mount / "formulas" / "double.txt").write_text("X\nV*2\n")
    state = {"mount": None}
    app = App(FixedAdc(), SimDisplay(tmp_path / "f.png"), NullPad(), tmp_path / "home",
              usb_finder=lambda: state["mount"])
    app.settings.formulas = ["Raw", "Raw"]
    app.step()
    assert not (tmp_path / "home" / "formulas" / "double.txt").exists()

    state["mount"] = mount
    app.step()  # insertion -> import
    assert "double" in app.formulas
    assert app._usb_sink is None  # logging not enabled yet

    # menu: open, go to "USB log" (index 5), toggle on
    app.ui.handle(Button.A)
    for _ in range(5):
        app.ui.handle(Button.DOWN)
    app.ui.handle(Button.RIGHT)
    assert app.settings.usb_logging and app._usb_sink is not None
    app.ui.handle(Button.B)
    app.step()
    app.step()

    state["mount"] = None
    app.step()  # removal closes sink
    assert app._usb_sink is None
    logs = list((mount / "voltmeter_logs").glob("*.csv"))
    assert len(logs) == 1
    assert len(list(csv.reader(open(logs[0])))) >= 2  # header + first record
    assert Settings.load(app.settings_path).usb_logging


def test_menu_changes_formula_and_value(tmp_path):
    app = App(FixedAdc(), SimDisplay(tmp_path / "f.png"), NullPad(), tmp_path / "home")
    (app.formula_dir).mkdir(parents=True)
    (app.formula_dir / "mv.txt").write_text("mV\nV*1000\n")
    app.formulas = load_formulas(app.formula_dir)
    app.ui.handle(Button.A)
    app.ui.handle(Button.RIGHT)  # Ch0: Raw -> next in sorted(["Raw","mv"]) = "mv"
    assert app.settings.formulas[0] == "mv"
    app.step()
    assert app.last_reading.channels[0].value == pytest.approx(1000)
    app.ui.render(app.last_reading, __import__("voltmeter.ui", fromlist=["Status"]).Status()).save(tmp_path / "x.png")


def test_records_every_n_seconds(tmp_path, monkeypatch):
    import voltmeter.app as appmod
    clock = {"t": 100.0}
    monkeypatch.setattr(appmod.time, "monotonic", lambda: clock["t"])
    app = App(FixedAdc(), SimDisplay(tmp_path / "f.png"), NullPad(), tmp_path / "home")
    got = []

    class Rec:
        def write(self, r):
            got.append(r)

        def close(self):
            pass

    app.add_sink(Rec())
    app.settings.n = 5
    for _ in range(12):  # display ticks every 1s
        app.step()
        clock["t"] += 1
    assert len(got) == 3  # t=0, 5, 10


def test_shipped_config_matches_defaults():
    assert Config.load(Path(__file__).parent.parent / "config.yaml") == Config()


def test_config_overrides_and_missing(tmp_path):
    assert Config.load(tmp_path / "nope.yaml") == Config()
    p = tmp_path / "c.yaml"
    p.write_text("device:\n  adc: {address: 0x49}\n  buttons: {up: 5}\n  bogus: 1\n"
                 "database: {host: db.example, enabled: true, flush_interval_s: 2}\n")
    c = Config.load(p)
    assert c.device.adc.address == 0x49 and c.device.adc.bus == 1
    assert c.device.buttons.up == 5 and c.device.buttons.down == 22
    assert c.database.host == "db.example" and c.database.enabled
    assert c.database.flush_interval_s == 2.0 and c.database.port == 5432


@pytest.mark.parametrize("text", ["device: 3", "device:\n  adc: {address: abc}", "- a", "a: [",
                                  "device:\n  usb: {mount_roots: x}", "database: {enabled: yes_please}",
                                  "database: {batch_size: 1.5}"])
def test_config_invalid(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text)
    with pytest.raises(ValueError):
        Config.load(p)


def test_usb_config_used(tmp_path):
    from voltmeter.config import UsbConfig
    m = tmp_path / "mounts"
    m.write_text("/dev/sdb1 /data/stick vfat rw 0 0\n")
    assert find_usb_mount(mounts_file=str(m)) is None
    assert str(find_usb_mount(UsbConfig(mount_roots=["/data/"]), str(m))) == "/data/stick"


class FixedAdc:
    def read_volts(self, channel, n, range_v):
        return 1.0

    def close(self):
        pass


def test_divider_scales_channels(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("device:\n  adc:\n    ch0_divider: {r_top: 10, r_bottom: 1}\n")
    app = App(FixedAdc(), SimDisplay(), NullPad(), tmp_path, Config.load(p))
    r = app.sample()
    assert r.channels[0].volts == pytest.approx(11.0)
    assert r.channels[1].volts == pytest.approx(1.0)


def test_divider_invalid(tmp_path):
    from voltmeter.config import DividerConfig
    with pytest.raises(ValueError):
        DividerConfig(r_top=1, r_bottom=0).ratio
