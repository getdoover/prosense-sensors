# ProSense Sensors

**Doover device driver for ProSense (H-Link / hlcode.cn) Modbus RTU sensors — temperature first, with the wider range scaffolded.**

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

[Getting Started](#getting-started) • [Configuration](#configuration) • [Tags](#tags) • [Hardware](#hardware) • [Modbus register map](#modbus-register-map) • [Developer](DEVELOPMENT.md)

## Overview

Reads physical measurements from ProSense Modbus RTU sensors and publishes them as Doover tags. The whole ProSense range — temperature probes (e.g. **SBWR-LED**), pressure transmitters, level transmitters, pH, etc. — speaks the same protocol; the unit code at register `0x0002` is what tells you which physical quantity is on the wire.

This first release fully implements the **temperature** variant. The `Variant` enum and unit-code map are scaffolded for the rest of the range so that adding pressure / level / humidity / pH / weight is a localised change.

The app talks to the sensor through pydoover's `ModbusInterface`, sharing the physical RS-485 bus with other Doover apps on the same gateway — it does not open the serial port directly.

### Product info

- Product page (Chinese): <https://h5.hlcode.com.cn/?id=NK1LFtJ&f=wx>
- Modbus communication manual (PDF): <https://oss.hlcode.cn/server/code/file/2024/12/10/16191437364738.pdf>

## Getting Started

1. Wire the sensor to the gateway's RS-485 port (see [Hardware](#hardware)).
2. Install this app onto the target device via the Doover platform.
3. At minimum set `Variant`, `Slave ID`, and the serial port fields inside `Modbus Config`. Defaults match the sensor's factory settings (9600 8N1, slave ID 1).
4. Watch the `temperature` tag populate. The `sensor_unit_label` tag shows the unit the sensor is configured for (`°C` or `°F`); the app converts to your chosen `display_unit` automatically.

### Local testing

```bash
uv sync
uv run pytest tests -v
doover app run
```

`doover app run` brings up the bundled simulator via docker-compose. With `sim_app_key` set (as in `simulators/app_config.json`), the app reads ProSense-shaped raw register values from the simulator's tags instead of the Modbus bus, so no hardware is required.

## Configuration

Fields are declared in [`src/prosense_sensors/app_config.py`](src/prosense_sensors/app_config.py). Run `uv run export-config && uv run export-ui` after any change to regenerate `doover_config.json`.

| Setting | Description | Default |
|---|---|---|
| **Sensor Variant** | `temperature`, `pressure`, `level`, `humidity`, `ph`, `weight`. Only `temperature` is fully wired up today. | `temperature` |
| **Modbus Config** | Nested `ModbusConfig` — bus type, serial port, baud, parity, etc. Defaults match ProSense factory settings. | serial / 9600 8N1 |
| **Slave ID** | Modbus unit ID of the sensor (1–255). | `1` |
| **Poll Interval (seconds)** | Time between Modbus reads. | `3` |
| **Display Unit (temperature)** | `celsius`, `fahrenheit`, or `kelvin`. The app converts from whatever unit the sensor is configured for. | `celsius` |
| **No-Comms Timeout (seconds)** | Clears `comms_ok` after this many seconds without a successful read. | `30` |
| **Over-Temperature Warning Threshold** | Optional. If set, a UI warning is shown when the published temperature ≥ this value. In the configured `display_unit`. | *(empty — disabled)* |
| **Enable UI** | Show this app's UI tile on the device. Disable to hide all UI elements (tags still publish). | `true` |
| **Temperature Ranges** | Optional list of coloured bands. Each entry has `label`, `range_min`, `range_max`, `colour`. **If any are set, the temperature is rendered as a radial gauge** with these bands; otherwise it's a plain numeric display. | *(empty)* |
| **Simulator App Key** | Optional. If set, read from the named simulator's tags instead of Modbus. Leave blank in production. | *(empty)* |

#### Example: temperature ranges

```json
"temperature_ranges": [
    {"label": "Cold",   "range_min": -20, "range_max": 0,  "colour": "blue"},
    {"label": "Normal", "range_min": 0,   "range_max": 30, "colour": "green"},
    {"label": "Hot",    "range_min": 30,  "range_max": 60, "colour": "red"}
]
```

`colour` accepts any HTML colour name (`red`, `orange`, `yellow`, `green`, `blue`, `purple`, `grey`, etc.) or a hex string like `#FF5733`. Bounds are in the configured `display_unit`.

## Tags

### Reading

| Tag | Type | Description |
|---|---|---|
| `temperature` | number | Temperature in the configured `display_unit`. |

### Diagnostics

| Tag | Type | Description |
|---|---|---|
| `comms_ok` | boolean | `true` while the sensor is responding. Cleared after `no_comms_timeout_seconds`. |
| `temperature_ok` | boolean | `true` while the published temperature is below `over_temp_threshold` (or no threshold is set). Drives the over-temperature warning indicator. |
| `last_read_time` | number | Unix timestamp of the last successful read. |
| `pv_native` | number | PV decoded from the float register, in the sensor's *native* unit (before display-unit conversion). |
| `sensor_unit_code` | integer | Raw contents of holding register `0x0002`. `20` = °C, `22` = °F (full table in the manual). |
| `sensor_unit_label` | string | Human label for `sensor_unit_code` (e.g. `°C`, `kPa`). |
| `decimal_point` | integer | Raw contents of holding register `0x0003` (0–4). Position of the decimal point applied to `raw_pv_int16`. |
| `raw_pv_int16` | integer | Sign-extended raw int16 PV from holding register `0x0004`. |
| `raw_pv_float_msw` | integer | High word of the IEEE754 float at register `0x0016`. |
| `raw_pv_float_lsw` | integer | Low word of the IEEE754 float at register `0x0017`. |

### Technician commissioning (tag-only — no UI)

Set any of these tags to a non-null value and the app will issue the matching Modbus write to the sensor, then clear the tag and report the result in `last_cmd_result`. **Power-cycle the sensor for the change to take effect.**

| Tag | Type | Notes |
|---|---|---|
| `cmd_set_slave_id` | integer | 1–255. Writes register `0x0000`. |
| `cmd_set_baud` | integer | One of `1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200`. Writes register `0x0001` as the manual's baud-code (0–7). |
| `cmd_set_parity` | string | `none`, `odd`, or `even`. Writes register `0x0025`. |
| `last_cmd_result` | string | Populated by the app: `ok: …` or `error: …`. |

In simulator mode (`sim_app_key` set) these writes are rejected so you can't accidentally "reconfigure" a sensor that isn't there.

### UI warnings

Two `WarningIndicator` elements are baked into the UI; they auto-hide via tag bindings, so the application just keeps the underlying tags up to date:

| Warning | Visible when | Bound tag |
|---|---|---|
| **Sensor Not Communicating** | `comms_ok` is false (no reads inside `no_comms_timeout_seconds`). | `comms_ok` |
| **Over Temperature** | `temperature ≥ over_temp_threshold`. Always hidden if the threshold is left blank. | `temperature_ok` |

## Hardware

**Bench reference:** **SBWR-LED** temperature transmitter (PT100 RTD with LED display, RS-485 Modbus RTU output).

**Factory serial defaults:** 9600 baud, 8 data bits, no parity, 1 stop bit, slave ID `0x01`.

Wire A/B of the sensor to the gateway's RS-485 A/B respectively, with GND tied between the gateway and the sensor's V−. Terminate the bus per the usual RS-485 rules.

## Modbus register map

The full protocol comes from the [Modbus comms manual](https://oss.hlcode.cn/server/code/file/2024/12/10/16191437364738.pdf). Function codes used: `0x03` (read holding), `0x04` (read input — equivalent), `0x06` (write single), `0x10` (write multiple). CRC poly `0xA001`.

| Address | FC | Type | Meaning |
|---|---|---|---|
| `0x0000` | 03 / 06 | uint16 | Slave ID (1–255) |
| `0x0001` | 03 / 06 | uint16 | Baud rate code: `0=1200, 1=2400, 2=4800, 3=9600, 4=19200, 5=38400, 6=57600, 7=115200` |
| `0x0002` | 03 / 06 | uint16 | Measurement unit code (e.g. `1=kPa, 6=PSI, 16=m, 20=°C, 22=°F`; full table in `prosense_driver.UNIT_LABELS`) |
| `0x0003` | 03 / 06 | uint16 | Decimal-point position for `0x0004` (`0=####`, `1=###.#`, …, `4=.####`) |
| `0x0004` | 03 | int16 | Primary measured value (PV), scale by `0x0003` |
| `0x0005` | 03 | int16 | Range zero point |
| `0x0006` | 03 | int16 | Range full point |
| `0x000C` | 03 / 06 | int16 | Zero-bit calibration offset (factory: 0) |
| `0x000F` | 06 | uint16 | Write `0` → save user area |
| `0x0010` | 06 | uint16 | Write `1` → restore factory parameters |
| `0x0016`–`0x0017` | 03 | float | PV as IEEE754 4-byte float, ABCD big-endian (high word first) |
| `0x0025` | 03 / 06 | uint16 | Parity (`0=none, 1=odd, 2=even`) |

This app reads two short blocks every poll:

1. `0x0002`–`0x0003` (unit + decimal point) for diagnostics and unit detection.
2. `0x0016`–`0x0017` (float PV) for the published value — the float gives ~7 significant digits, more precision than the int16 PV at `0x0004` paired with the decimal-point register. The int16 PV is also read once per poll as a fallback / cross-check.

## Project layout

```
src/prosense_sensors/
  __init__.py          # Entry point — run_app(ProSenseApplication())
  application.py       # Main loop, Modbus I/O, sim path, technician commands
  app_config.py        # Config schema (incl. ModbusConfig and Variant enum)
  app_tags.py          # Tag declarations
  app_ui.py            # UI definition
  prosense_driver.py   # Pure decode/encode helpers + register map
simulators/sample/     # ProSense-shaped tag simulator
tests/                 # pytest suite
```

## Need Help?

- Email: support@doover.com
- [Doover Documentation](https://docs.doover.com)

## License

Apache License 2.0 — see [LICENSE](LICENSE).
