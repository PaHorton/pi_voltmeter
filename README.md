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
  formula/log folder names on the stick, plus the `database:` section for TigerData publishing. Every key is optional; see [config.yaml](config.yaml) for defaults.
  Bad values stop startup with a clear message; unknown keys are warned about and ignored.
- `settings.json`: user preferences changed from the on-screen menu (not for hand editing).

## TigerData (Grafana dashboard backend)
1. Create a service at tigerdata.com and note its host, port, dbname and user.
2. In `config.yaml` set `database.enabled: true` and `database.host` (plus the other fields).
3. Install the driver: `pip install '.[tigerdata]'`.
4. Provide the password via environment, never in the file: `export VOLTMETER_DB_PASSWORD=...`
   (for systemd, put `VOLTMETER_DB_PASSWORD=...` in `/etc/voltmeter.env`, `chmod 600`; the unit loads it if present).

The table (`readings` by default) is created automatically as a hypertable, one row per channel
per record (every N seconds): `time, device_id, channel, volts, unit, formula`. `volts` is the input voltage (after the
divider); the formula is a label only, so apply it in the query (e.g. `10 ^ ((volts - 7.75)/0.75 - 0.125)`).
`device_id` defaults to the hostname, so several devices can share a table. In Grafana add a
PostgreSQL/TimescaleDB data source and query, e.g.:
```sql
SELECT time, volts FROM readings
WHERE device_id = 'pi-1' AND channel = 0 AND $__timeFilter(time) ORDER BY time
```
Uploads run on a background thread in batches. If the network drops, rows buffer in memory
(up to `max_buffer`, oldest dropped beyond) and are sent on reconnect; duplicates are
impossible on retry. The buffer is **not** persisted, so a power loss while offline loses the
unsent rows (the USB CSV is the durable copy).

## Pi setup
```
sudo raspi-config nonint do_i2c 0        # enable I2C
python3 -m venv ~/voltmeter-venv && ~/voltmeter-venv/bin/pip install '.[pi]'
mkdir -p ~/voltmeter/formulas && cp formulas/*.txt ~/voltmeter/formulas/ && cp config.yaml ~/voltmeter/
# edit User/paths in deploy/voltmeter.service if your user is not "admin"
sudo cp deploy/voltmeter.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now voltmeter
```
- Defaults: ADS1115 `0x48`, OLED `0x3C`; change them in `config.yaml`.
- Input dividers: set `device.adc.ch0_divider` / `ch1_divider` (`r_top`, `r_bottom`) if a resistor divider sits ahead of an ADC input. Readings, formulas and logs then see the real input voltage (pin voltage × (r_top + r_bottom) / r_bottom). The menu "Range" is the ADC pin range, not the input range.
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

## Web dashboard (Vercel)
`web/` is a FastAPI app that shows the latest value of every channel, refreshed every 2 s
(`/` is the page, `/api/latest` the JSON). It reads the `readings` table and applies each row's
stored formula name to its `volts` using the same `formulas/*.txt` and parser as the Pi.

1. In `psql` (as the admin user) run `db/readonly_role.sql` after setting a password; the web app
   gets a SELECT-only role, never the admin or Pi credentials.
2. Run locally:
   `DATABASE_URL='postgres://voltmeter_web:PASSWORD@HOST:PORT/tsdb?sslmode=require' uvicorn app:app --reload`
   from `web/` (needs `pip install '.[web]'`).
3. Deploy: import the GitHub repo in Vercel, set **Root Directory** to `web`, keep "Include source
   files outside of the Root Directory" enabled (the app reads `../formulas` and `../voltmeter`),
   and add the `DATABASE_URL` environment variable. Optional: `READINGS_TABLE`, `STALE_AFTER_S`.
Channels older than `STALE_AFTER_S` (30 s) are dimmed and marked stale. If a row names a formula
that is not in `formulas/`, the page falls back to showing volts.
