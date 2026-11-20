"""Opt-in lab logger. The original voltmeter command remains unchanged."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
from pathlib import Path

from .adc import Ads1115, SimAdc
from .app import App, NullPad, SimDisplay
from .catalog import load_catalog
from .config import Config
from .logger_config import load_logger_config, validate_catalog
from .lakeshore218 import LakeShore218, SerialSettings
from .lakeshore_plugin import LakeShorePlugin
from .measurement_sink import MeasurementSink
from .runtime import PluginRuntime, PluginSchedule
from .voltmeter_plugin import VoltmeterPlugin

log = logging.getLogger(__name__)


class ReadingSink:
    """Bridge the OLED app's N-cadence readings to generic storage."""

    def __init__(self, plugin: VoltmeterPlugin, sink: MeasurementSink):
        self.plugin, self.sink = plugin, sink

    def write(self, reading):
        for measurement in self.plugin.measurements(reading):
            self.sink.write(measurement)

    def close(self):
        pass  # The lab logger owns the shared sink.


def database_sink(config):
    if not config.enabled or not config.host:
        raise ValueError("lab logger requires database.enabled and database.host")
    password = os.environ.get(config.password_env)
    if not password:
        raise ValueError(f"set database password in ${config.password_env}")
    try:
        import psycopg
    except ImportError as exc:
        raise ValueError("install the TigerData extra: pip install '.[tigerdata]'") from exc

    def connect():
        return psycopg.connect(host=config.host, port=config.port, dbname=config.dbname,
                               user=config.user, password=password, sslmode=config.sslmode,
                               connect_timeout=10, options="-c statement_timeout=10000")

    return MeasurementSink(connect, batch_size=config.batch_size, max_buffer=config.max_buffer,
                           flush_interval_s=config.flush_interval_s)


def lakeshore_schedule(instrument, logger_id):
    options = instrument.options
    keys = {"port", "baudrate", "bytesize", "parity", "stopbits", "timeout"}
    if set(options) != keys:
        raise ValueError("Lake Shore options require port, baudrate, bytesize, parity, stopbits and timeout")
    port = options["port"]
    if not isinstance(port, str) or not port.strip():
        raise ValueError("Lake Shore port must be a non-empty string")
    settings = SerialSettings(baudrate=options["baudrate"], bytesize=options["bytesize"],
                              parity=options["parity"], stopbits=options["stopbits"],
                              timeout=options["timeout"])
    plugin = LakeShorePlugin(lambda: LakeShore218(port, settings), logger_id,
                             instrument.system_id, instrument.id, instrument.sensors)
    return PluginSchedule(instrument.id, plugin, instrument.interval_s)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lab measurement logger")
    parser.add_argument("--config", type=Path, required=True, help="logger configuration YAML")
    parser.add_argument("--catalog", type=Path, required=True, help="public catalog YAML")
    parser.add_argument("--home", type=Path,
                        default=Path(os.environ.get("VOLTMETER_HOME", "~/voltmeter")).expanduser())
    parser.add_argument("--device-config", type=Path, help="existing Pi hardware/database YAML")
    parser.add_argument("--sim", action="store_true", help="simulate the voltmeter hardware")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        logger = load_logger_config(args.config)
        validate_catalog(logger, load_catalog(args.catalog))
        if not logger.instruments:
            raise ValueError("logger configuration must declare at least one instrument")
        config = Config.load(args.device_config or args.home / "config.yaml")
        voltmeters = [instrument for instrument in logger.instruments if instrument.type == "voltmeter"]
        if len(voltmeters) > 1:
            raise ValueError("one OLED voltmeter plugin is supported per logger")
        schedules = [lakeshore_schedule(instrument, logger.logger_id)
                     for instrument in logger.instruments if instrument.type == "lakeshore218"]
        if voltmeters and (voltmeters[0].options or set(voltmeters[0].sensors) != {0, 1}):
            raise ValueError("voltmeter requires channels 0 and 1 and empty options")
        sink = database_sink(config.database)
    except (ValueError, OSError) as exc:
        raise SystemExit(f"lab configuration error: {exc}") from exc

    app = None
    devices = []
    def publish(measurements):
        for measurement in measurements:
            sink.write(measurement)

    runtime = PluginRuntime(schedules, publish)
    stop = threading.Event()
    previous_handlers = {}

    def request_stop(signum, frame):
        stop.set()
        if app is not None:
            app.running = False

    try:
        if voltmeters:
            instrument = voltmeters[0]
            if args.sim:
                adc, display, pad = SimAdc(), SimDisplay(), NullPad()
                devices = [adc, display, pad]
            else:
                from .hardware import ButtonPad, Oled
                adc = Ads1115(config.device.adc)
                devices.append(adc)
                display = Oled(config.device.display)
                devices.append(display)
                pad = ButtonPad(config.device.buttons)
                devices.append(pad)
            app = App(adc, display, pad, args.home, config)
            plugin = VoltmeterPlugin(adc, app.settings, app._divider, lambda: app.formulas,
                                     logger.logger_id, instrument.system_id, instrument.id,
                                     [instrument.sensors[0], instrument.sensors[1]])
            app.voltmeter = plugin
            app.add_sink(ReadingSink(plugin, sink))
            log.info("voltmeter record cadence follows OLED N (%s seconds)", app.settings.n)
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, request_stop)
        runtime.start()
        if app is not None:
            app.run()
        else:
            stop.wait()
    finally:
        try:
            runtime.close()
        finally:
            if app is not None:
                app.close()
            for device in devices:
                device.close()
            sink.close()
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    main()
