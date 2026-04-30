"""Pure decode/encode helpers for the ProSense (H-Link / hlcode.cn) sensor family.

The whole ProSense range — pressure transmitters, level transmitters,
temperature probes (incl. SBWR-LED), pH sensors, etc. — speaks the same
Modbus RTU protocol. Only the unit code at register 0x0002 distinguishes
them, plus the physical scaling. This module covers the common register
map; the per-variant flavour (which units to expect, what to call the
reading) is selected via :class:`Variant`.

Register map (holding registers, FC 0x03 read / 0x06 write):

    0x0000  uint16   slave id (1..255)
    0x0001  uint16   baud-rate code (0..7 → 1200..115200)
    0x0002  uint16   measurement unit code (see UNIT_LABELS)
    0x0003  uint16   decimal-point position (0=#### .. 4=.####)
    0x0004  int16    primary measured value (PV), scaled by reg 0x0003
    0x0005  int16    range zero point
    0x0006  int16    range full point
    0x000C  int16    zero-bit offset (calibration trim, 0 from factory)
    0x000F  uint16   write 0 → save user area
    0x0010  uint16   write 1 → restore factory parameters
    0x0016  float    PV as 4-byte IEEE754 (ABCD big-endian, 2 regs)
    0x0025  uint16   parity (0=none, 1=odd, 2=even)

We default to reading the float at 0x0016 — it's higher precision than
the int16+decimal-point pair and avoids a register-pair race. The int16
PV at 0x0004 is still surfaced as a diagnostic.
"""

from __future__ import annotations

import enum
import math
import struct
from dataclasses import dataclass

# ---- register addresses ------------------------------------------------

REG_SLAVE_ID = 0x0000
REG_BAUD = 0x0001
REG_UNIT = 0x0002
REG_DECIMAL_POINT = 0x0003
REG_PV_INT16 = 0x0004
REG_RANGE_ZERO = 0x0005
REG_RANGE_FULL = 0x0006
REG_ZERO_OFFSET = 0x000C
REG_SAVE_USER = 0x000F
REG_RESTORE_FACTORY = 0x0010
REG_PV_FLOAT = 0x0016
REG_PARITY = 0x0025

# Block reads — keep these as "what the application asks for in one go"
META_BLOCK_START = REG_UNIT          # 0x0002
META_BLOCK_COUNT = 2                 # unit + decimal_point

PV_FLOAT_BLOCK_START = REG_PV_FLOAT  # 0x0016
PV_FLOAT_BLOCK_COUNT = 2             # 2 × uint16 = 4 bytes

PV_INT16_BLOCK_START = REG_PV_INT16  # 0x0004
PV_INT16_BLOCK_COUNT = 1

# ---- variants ---------------------------------------------------------

class Variant(str, enum.Enum):
    """ProSense product family.

    Only TEMPERATURE is fully wired up at the moment. The rest are
    listed so that adding them later is a localised change (extend
    EXPECTED_UNIT_CODES, the display-unit options for that variant,
    and any per-variant tag naming).
    """

    TEMPERATURE = "temperature"   # e.g. SBWR-LED probe
    PRESSURE = "pressure"
    LEVEL = "level"
    HUMIDITY = "humidity"
    PH = "ph"
    WEIGHT = "weight"


# Unit codes the manual lists at register 0x0002. Index = code.
UNIT_LABELS: dict[int, str] = {
    0: "MPa",
    1: "kPa",
    2: "Pa",
    3: "bar",
    4: "mbar",
    5: "kgf/cm²",
    6: "PSI",
    7: "mH₂O",
    8: "mmH₂O",
    9: "inH₂O",
    10: "H₂O",
    11: "mHg",
    12: "mmHg",
    13: "inHg",
    14: "atm",
    15: "Torr",
    16: "m",
    17: "cm",
    18: "mm",
    19: "kg",
    20: "°C",
    21: "pH",
    22: "°F",
    23: "",  # "Empty" / unitless
}


# Which on-sensor unit codes are sane for each variant. Used to flag
# obviously-wrong configurations (e.g. a pressure unit reported by what
# is supposed to be a temperature probe).
EXPECTED_UNIT_CODES: dict[Variant, frozenset[int]] = {
    Variant.TEMPERATURE: frozenset({20, 22}),                 # °C, °F
    Variant.PRESSURE:    frozenset({0, 1, 2, 3, 4, 5, 6}),     # MPa..PSI
    Variant.LEVEL:       frozenset({7, 8, 9, 10, 16, 17, 18}), # water columns + length
    Variant.HUMIDITY:    frozenset({23}),                      # often unitless / %RH
    Variant.PH:          frozenset({21}),
    Variant.WEIGHT:      frozenset({19}),
}


def unit_label(code: int | None) -> str:
    if code is None:
        return ""
    return UNIT_LABELS.get(int(code), f"unit#{int(code)}")


# ---- decoded readings -------------------------------------------------

@dataclass(frozen=True)
class MetaReading:
    """Result of reading the META block (regs 0x0002..0x0003)."""

    unit_code: int | None
    decimal_point: int | None

    @property
    def unit_label(self) -> str:
        return unit_label(self.unit_code)


@dataclass(frozen=True)
class PVReading:
    """Result of reading the PV registers.

    ``value_native`` is the physical reading in the *sensor's* configured
    unit (whatever ``MetaReading.unit_code`` says). The application is
    responsible for converting to a display unit if the user asked for
    one. ``raw_int16`` is the int16 PV from 0x0004 if it was read; useful
    as a diagnostic / fallback when the float register is empty.
    """

    value_native: float | None
    raw_float_words: tuple[int, int] | None
    raw_int16: int | None


# ---- decoders ---------------------------------------------------------

def to_signed16(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value >= 0x8000 else value


def decode_meta(regs: list[int] | int | None) -> MetaReading:
    """Decode a META block (regs 0x0002..0x0003)."""
    values = _normalise(regs)
    if values is None or len(values) < 2:
        return MetaReading(None, None)
    return MetaReading(unit_code=int(values[0]) & 0xFFFF, decimal_point=int(values[1]) & 0xFFFF)


def decode_pv_float(regs: list[int] | int | None) -> float | None:
    """Decode a 2-register IEEE754 float at 0x0016, ABCD byte order.

    ABCD = high word first, big-endian within each word — the natural
    big-endian float layout. Returns None for NaN / Inf or short reads.
    """
    values = _normalise(regs)
    if values is None or len(values) < 2:
        return None
    high = int(values[0]) & 0xFFFF
    low = int(values[1]) & 0xFFFF
    raw = struct.pack(">HH", high, low)
    (value,) = struct.unpack(">f", raw)
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def decode_pv_int16(raw: list[int] | int | None, decimal_point: int | None) -> float | None:
    """Decode the int16 PV at 0x0004 using the decimal-point register."""
    values = _normalise(raw)
    if values is None or len(values) < 1:
        return None
    if decimal_point is None or decimal_point < 0 or decimal_point > 4:
        return None
    return to_signed16(int(values[0])) / (10 ** int(decimal_point))


def _normalise(regs: list[int] | int | None) -> list[int] | None:
    if regs is None:
        return None
    if isinstance(regs, int):
        return [regs]
    return list(regs)


# ---- temperature unit conversion --------------------------------------
#
# For the TEMPERATURE variant the sensor's configured unit (reg 0x0002)
# is either °C (20) or °F (22). The user's preferred display unit is
# independent — ``convert_temperature`` handles the cross-product.

class TemperatureUnit(str, enum.Enum):
    CELSIUS = "celsius"
    FAHRENHEIT = "fahrenheit"
    KELVIN = "kelvin"


_TEMP_UNIT_BY_CODE = {20: TemperatureUnit.CELSIUS, 22: TemperatureUnit.FAHRENHEIT}


def temperature_unit_from_code(unit_code: int | None) -> TemperatureUnit | None:
    if unit_code is None:
        return None
    return _TEMP_UNIT_BY_CODE.get(int(unit_code))


def convert_temperature(value: float, src: TemperatureUnit, dst: TemperatureUnit) -> float:
    if src is dst:
        return value
    # Normalise to °C, then convert out.
    if src is TemperatureUnit.FAHRENHEIT:
        celsius = (value - 32.0) * 5.0 / 9.0
    elif src is TemperatureUnit.KELVIN:
        celsius = value - 273.15
    else:
        celsius = value

    if dst is TemperatureUnit.FAHRENHEIT:
        return celsius * 9.0 / 5.0 + 32.0
    if dst is TemperatureUnit.KELVIN:
        return celsius + 273.15
    return celsius


def temperature_unit_label(unit: TemperatureUnit) -> str:
    return {
        TemperatureUnit.CELSIUS: "°C",
        TemperatureUnit.FAHRENHEIT: "°F",
        TemperatureUnit.KELVIN: "K",
    }[unit]


# ---- commissioning encoders -------------------------------------------
#
# The manual's baud register stores a small integer code, NOT the raw
# baud or the BCD form some sensors use. 0=1200, 1=2400, ..., 7=115200.

_BAUD_CHOICES: dict[int, int] = {
    1200: 0,
    2400: 1,
    4800: 2,
    9600: 3,
    19200: 4,
    38400: 5,
    57600: 6,
    115200: 7,
}

_PARITY_CHOICES = {"none": 0, "odd": 1, "even": 2}


def encode_baud(baud: int) -> int:
    if baud not in _BAUD_CHOICES:
        raise ValueError(
            f"Unsupported baud {baud!r}; expected one of {sorted(_BAUD_CHOICES)}"
        )
    return _BAUD_CHOICES[baud]


def encode_parity(parity: str) -> int:
    key = (parity or "").strip().lower()
    if key not in _PARITY_CHOICES:
        raise ValueError(
            f"Unsupported parity {parity!r}; expected one of {list(_PARITY_CHOICES)}"
        )
    return _PARITY_CHOICES[key]


def validate_slave_id(slave_id: int) -> int:
    if not isinstance(slave_id, int) or slave_id < 1 or slave_id > 255:
        raise ValueError(f"Slave id must be an int in 1..255, got {slave_id!r}")
    return slave_id


def encode_pv_float(value: float) -> tuple[int, int]:
    """Inverse of :func:`decode_pv_float` — used by the simulator."""
    raw = struct.pack(">f", float(value))
    high, low = struct.unpack(">HH", raw)
    return high, low
