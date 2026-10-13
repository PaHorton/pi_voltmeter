"""Application wiring: sample -> convert -> fan out to sinks and display."""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from .adc import Adc, Ads1115, SimAdc
from .config import Config
from .formulas import RAW, load_formulas
from .settings import NUM_CHANNELS, Settings
from .sinks import ChannelReading, CsvSink, Reading, Sink
from .ui import Button, Status, Ui
from .usb import UsbMonitor, find_usb_mount, import_formulas

log = logging.getLogger("voltmeter")

AVG_SAMPLES = 4  # ADC samples averaged per displayed reading (noise reduction)


class App:
    def __init__(self, adc: Adc, display, pad, home: Path, config: Optional[Config] = None,
                 usb_finder: Optional[Callable[[], Optional[Path]]] = None):
        self.adc, self.display, self.pad = adc, display, pad
        self.config = config or Config()
        if usb_finder is None:
            usb_finder = lambda: find_usb_mount(self.config.device.usb)  # noqa: E731
        self.home = home
        self.formula_dir = home / "formulas"
        self.settings_path = home / "settings.json"
        self.settings = Settings.load(self.settings_path)
        self.formulas = load_formulas(self.formula_dir)
        self.usb = UsbMonitor(usb_finder)
        self.ui = Ui(self.settings, lambda: sorted(self.formulas), self._settings_changed)
        self._sinks: List[Sink] = []   # extension point: add_sink(TigerDataSink(...))
        self._usb_sink: Optional[CsvSink] = None
        self.last_reading: Optional[Reading] = None
        self._last_record: Optional[float] = None  # monotonic time of last record
        self.running = True

    # ---- extension point ---------------------------------------------
    def add_sink(self, sink: Sink) -> None:
        self._sinks.append(sink)

    # ---- settings / USB ----------------------------------------------
    def _settings_changed(self) -> None:
        self.settings.save(self.settings_path)
        self._sync_usb_sink()

    def _sync_usb_sink(self) -> None:
        want = self.settings.usb_logging and self.usb.mount is not None
        if want and self._usb_sink is None:
            try:
                self._usb_sink = CsvSink(self.usb.mount / self.config.device.usb.log_dir,
                                         NUM_CHANNELS)
                self._last_record = None  # record immediately into the new file
                log.info("logging to %s", self._usb_sink.path)
            except OSError as exc:
                log.error("cannot start USB log: %s", exc)
                self.ui.toast("USB write failed")
        elif not want and self._usb_sink is not None:
            self._usb_sink.close()
            self._usb_sink = None

    def _poll_usb(self) -> None:
        inserted, removed = self.usb.poll()
        if removed:
            self._sync_usb_sink()
            self.ui.toast("USB removed")
        if inserted:
            count = import_formulas(inserted, self.formula_dir,
                                    self.config.device.usb.formulas_dir)
            if count:
                self.formulas = load_formulas(self.formula_dir)
            self.ui.toast(f"USB: {count} formulas")
            self._sync_usb_sink()

    # ---- sampling ----------------------------------------------------
    def sample(self) -> Reading:
        s = self.settings
        channels = []
        for ch in range(NUM_CHANNELS):
            formula = self.formulas.get(s.formulas[ch], RAW)
            volts = self.adc.read_volts(ch, AVG_SAMPLES, s.range_v)
            channels.append(ChannelReading(volts, formula(volts), formula.unit, formula.name))
        return Reading(datetime.now().astimezone(), channels)

    def _publish(self, reading: Reading) -> None:
        """Send to sinks once every N seconds (settings.n); display updates faster."""
        now = time.monotonic()
        if self._last_record is not None and now - self._last_record < self.settings.n:
            return
        self._last_record = now
        sinks = self._sinks + ([self._usb_sink] if self._usb_sink else [])
        for sink in sinks:
            try:
                sink.write(reading)
            except OSError as exc:  # e.g. stick yanked mid-write
                log.error("sink failed: %s", exc)
                if sink is self._usb_sink:
                    self._usb_sink.close()
                    self._usb_sink = None
                    self.ui.toast("USB write failed")

    def _render(self) -> None:
        status = Status(self.usb.mount is not None, self._usb_sink is not None)
        self.display.show(self.ui.render(self.last_reading, status))

    # ---- main loop ---------------------------------------------------
    def step(self) -> None:
        self._poll_usb()
        try:
            self.last_reading = self.sample()
        except OSError as exc:
            log.error("ADC read failed: %s", exc)
            self.last_reading = None
            self.ui.toast("ADC error")
        else:
            self._publish(self.last_reading)
        self._render()

    def run(self) -> None:
        self._sync_usb_sink()
        while self.running:
            deadline = time.monotonic() + self.settings.period_s
            self.step()
            while time.monotonic() < deadline:
                presses = self.pad.drain()
                for b in presses:
                    self.ui.handle(b)
                if presses:
                    self._render()
                time.sleep(0.02)

    def close(self) -> None:
        for sink in self._sinks + ([self._usb_sink] if self._usb_sink else []):
            sink.close()
        self.settings.save(self.settings_path)


class SimDisplay:
    """Writes the frame to a PNG so the UI can be viewed without hardware."""

    def __init__(self, path: Path = Path("/tmp/voltmeter.png")):
        self.path = path

    def show(self, image) -> None:
        image.resize((512, 256)).save(self.path)

    def close(self) -> None:
        pass


class NullPad:
    def drain(self) -> List[Button]:
        return []

    def close(self) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Pi voltmeter")
    parser.add_argument("--home", type=Path,
                        default=Path(os.environ.get("VOLTMETER_HOME", "~/voltmeter")).expanduser(),
                        help="folder holding settings.json, config.yaml and formulas/")
    parser.add_argument("--config", type=Path, help="config file (default: <home>/config.yaml)")
    parser.add_argument("--sim", action="store_true", help="no hardware; write /tmp/voltmeter.png")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        config = Config.load(args.config or args.home / "config.yaml")
    except ValueError as exc:
        raise SystemExit(f"config error: {exc}")

    if args.sim:
        adc, display, pad = SimAdc(), SimDisplay(), NullPad()
    else:
        from .hardware import ButtonPad, Oled
        dev = config.device
        adc, display, pad = Ads1115(dev.adc), Oled(dev.display), ButtonPad(dev.buttons)

    app = App(adc, display, pad, args.home, config)
    if config.database.enabled:
        from .tigerdata import build_sink
        try:
            app.add_sink(build_sink(config.database))
        except ValueError as exc:
            raise SystemExit(f"database config error: {exc}")
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        for dev in (adc, display, pad):
            dev.close()


if __name__ == "__main__":
    main()
