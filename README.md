# Pi voltmeter

CM4 + ADS1115 (2 channels, single-ended A0/A1) + Adafruit 128x64 OLED bonnet.

## Features
- Live value of both channels, converted by a selectable formula.
- Formula files: `<name>.txt`, line 1 = unit, line 2 = expression in `V`
  (e.g. `degC` / `V * 100`). Functions: sqrt, log, ln, log10, log2, exp, abs, sin, cos,
  tan, asin, acos, atan, pow, min, max, round; constants pi, e. Invalid files are skipped.
- On-screen menu: **A** or joystick press opens it, up/down selects, left/right changes a
  value, **B** goes back. Items: formula per channel, N (seconds between timestamped records in the CSV / future DB), ADC range,
  display refresh period, USB logging. Settings persist in `settings.json`.
- Plug in a USB stick: valid `formulas/*.txt` on it are copied to the Pi. With
  *USB log: ON*, readings are written to `voltmeter_logs/log_<time>.csv` on the stick
  (new file per insertion; header + one row per reading). Screen shows `REC` while logging.

## Configuration
- `config.yaml` (default `~/voltmeter/config.yaml`, or `--config`): device-level setup, read once
  at startup. Covers ADC/OLED I2C bus and address, button GPIO pins, USB mount roots and the
  formula/log folder names on the stick, plus a reserved `database:` section for future sinks
  (e.g. TigerData). Every key is optional; see [config.yaml](config.yaml) for defaults.
  Bad values stop startup with a clear message; unknown keys are warned about and ignored.
- `settings.json`: user preferences changed from the on-screen menu (not for hand editing).

## Pi setup
```
sudo raspi-config nonint do_i2c 0        # enable I2C
python3 -m venv ~/voltmeter-venv && ~/voltmeter-venv/bin/pip install '.[pi]'
mkdir -p ~/voltmeter/formulas && cp formulas/*.txt ~/voltmeter/formulas/ && cp config.yaml ~/voltmeter/
sudo cp deploy/voltmeter.service /etc/systemd/system/ && sudo systemctl enable --now voltmeter
```
- Defaults: ADS1115 `0x48`, OLED `0x3C`; change them in `config.yaml`.
- USB must be auto-mounted under a `usb.mount_roots` folder (`/media`, `/mnt`, `/run/media` by default) (Pi OS desktop does this;
  on Lite install `usbmount` or `udisks2`+`udiskie`). Remove the stick before unplugging
  to avoid data loss; the CSV is flushed every row and fsynced every second.
- Keep inputs within 0..VDD (+0.3 V) of the ADS1115.

## Develop without hardware
```
pip install -e '.[dev]' && pytest
voltmeter --sim --home /tmp/vm      # writes the screen to /tmp/voltmeter.png
```

## Layout / extending
- `adc.py` ADC backends · `formulas.py` · `settings.py` · `usb.py` · `ui.py` (renders a PIL image)
  · `hardware.py` (OLED/buttons) · `app.py` (loop).
- Every reading is a `Reading` object fanned out to **sinks** (`sinks.py`). For TigerData/Grafana,
  add a class with `write(reading)` / `close()` (batch + retry in a background thread so the
  sampling loop never blocks) and register it with `App.add_sink(...)`. Grafana reads
  from the TigerData (Postgres) table.
