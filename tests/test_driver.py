"""Tests for the pure prosense_driver decoding/encoding layer."""

import math
import struct

import pytest

from prosense_sensors.prosense_driver import (
    EXPECTED_UNIT_CODES,
    UNIT_LABELS,
    TemperatureUnit,
    Variant,
    convert_temperature,
    decode_meta,
    decode_pv_float,
    decode_pv_int16,
    encode_baud,
    encode_parity,
    encode_pv_float,
    temperature_unit_from_code,
    temperature_unit_label,
    to_signed16,
    unit_label,
    validate_slave_id,
)


# ---- decode_meta ------------------------------------------------------

def test_decode_meta_happy_path():
    meta = decode_meta([20, 1])
    assert meta.unit_code == 20
    assert meta.decimal_point == 1
    assert meta.unit_label == "°C"


def test_decode_meta_short_read_returns_blanks():
    meta = decode_meta([20])
    assert meta.unit_code is None
    assert meta.decimal_point is None


def test_decode_meta_none_is_blank():
    meta = decode_meta(None)
    assert meta.unit_code is None
    assert meta.decimal_point is None


# ---- decode_pv_float --------------------------------------------------

def test_decode_pv_float_round_trips():
    # 23.45 °C → encode → decode → 23.45
    msw, lsw = encode_pv_float(23.45)
    assert decode_pv_float([msw, lsw]) == pytest.approx(23.45, rel=1e-6)


def test_decode_pv_float_handles_negative():
    msw, lsw = encode_pv_float(-12.5)
    assert decode_pv_float([msw, lsw]) == pytest.approx(-12.5)


def test_decode_pv_float_abcd_byte_order():
    # 1.0 in IEEE754 big-endian is 0x3F800000.
    # ABCD: high word 0x3F80, low word 0x0000.
    assert decode_pv_float([0x3F80, 0x0000]) == pytest.approx(1.0)


def test_decode_pv_float_rejects_nan():
    nan_bytes = struct.pack(">f", float("nan"))
    msw, lsw = struct.unpack(">HH", nan_bytes)
    assert decode_pv_float([msw, lsw]) is None


def test_decode_pv_float_rejects_inf():
    inf_bytes = struct.pack(">f", float("inf"))
    msw, lsw = struct.unpack(">HH", inf_bytes)
    assert decode_pv_float([msw, lsw]) is None


def test_decode_pv_float_short_read_returns_none():
    assert decode_pv_float([0x3F80]) is None
    assert decode_pv_float(None) is None


# ---- decode_pv_int16 --------------------------------------------------

def test_decode_pv_int16_with_decimal_point():
    # raw 234 with dp=1 → 23.4
    assert decode_pv_int16([234], decimal_point=1) == pytest.approx(23.4)


def test_decode_pv_int16_negative_via_twos_complement():
    # -125 as uint16 = 0xFF83
    assert decode_pv_int16([0xFF83], decimal_point=1) == pytest.approx(-12.5)


def test_decode_pv_int16_dp_zero_is_integer():
    assert decode_pv_int16([100], decimal_point=0) == pytest.approx(100.0)


def test_decode_pv_int16_rejects_invalid_dp():
    assert decode_pv_int16([100], decimal_point=None) is None
    assert decode_pv_int16([100], decimal_point=-1) is None
    assert decode_pv_int16([100], decimal_point=5) is None


# ---- to_signed16 ------------------------------------------------------

def test_to_signed16_handles_full_range():
    assert to_signed16(0) == 0
    assert to_signed16(0x7FFF) == 32767
    assert to_signed16(0x8000) == -32768
    assert to_signed16(0xFFFF) == -1


# ---- unit labels ------------------------------------------------------

def test_unit_label_known_codes():
    assert unit_label(20) == "°C"
    assert unit_label(22) == "°F"
    assert unit_label(1) == "kPa"


def test_unit_label_unknown_code_does_not_crash():
    assert "99" in unit_label(99)


def test_unit_label_none_is_empty():
    assert unit_label(None) == ""


def test_unit_labels_table_covers_all_documented_codes():
    # The manual lists codes 0..23 — make sure none have been dropped.
    for code in range(24):
        assert code in UNIT_LABELS


# ---- temperature unit conversion -------------------------------------

def test_convert_temperature_identity():
    assert convert_temperature(25.0, TemperatureUnit.CELSIUS, TemperatureUnit.CELSIUS) == 25.0


def test_convert_temperature_celsius_fahrenheit():
    assert convert_temperature(0.0, TemperatureUnit.CELSIUS, TemperatureUnit.FAHRENHEIT) == pytest.approx(32.0)
    assert convert_temperature(100.0, TemperatureUnit.CELSIUS, TemperatureUnit.FAHRENHEIT) == pytest.approx(212.0)


def test_convert_temperature_fahrenheit_celsius():
    assert convert_temperature(32.0, TemperatureUnit.FAHRENHEIT, TemperatureUnit.CELSIUS) == pytest.approx(0.0)
    assert convert_temperature(212.0, TemperatureUnit.FAHRENHEIT, TemperatureUnit.CELSIUS) == pytest.approx(100.0)


def test_convert_temperature_celsius_kelvin():
    assert convert_temperature(0.0, TemperatureUnit.CELSIUS, TemperatureUnit.KELVIN) == pytest.approx(273.15)


def test_temperature_unit_from_code():
    assert temperature_unit_from_code(20) is TemperatureUnit.CELSIUS
    assert temperature_unit_from_code(22) is TemperatureUnit.FAHRENHEIT
    assert temperature_unit_from_code(1) is None  # kPa is not a temperature unit
    assert temperature_unit_from_code(None) is None


def test_temperature_unit_label():
    assert temperature_unit_label(TemperatureUnit.CELSIUS) == "°C"
    assert temperature_unit_label(TemperatureUnit.FAHRENHEIT) == "°F"
    assert temperature_unit_label(TemperatureUnit.KELVIN) == "K"


# ---- variant scaffolding ---------------------------------------------

def test_all_variants_have_expected_unit_codes():
    for v in Variant:
        assert v in EXPECTED_UNIT_CODES, f"{v} missing from EXPECTED_UNIT_CODES"


def test_temperature_expected_units_are_celsius_or_fahrenheit():
    assert EXPECTED_UNIT_CODES[Variant.TEMPERATURE] == frozenset({20, 22})


# ---- commissioning encoders ------------------------------------------

def test_encode_baud_known_rates():
    assert encode_baud(1200) == 0
    assert encode_baud(9600) == 3
    assert encode_baud(115200) == 7


def test_encode_baud_rejects_unknown():
    with pytest.raises(ValueError):
        encode_baud(1234)


def test_encode_parity_known_values():
    assert encode_parity("none") == 0
    assert encode_parity("ODD") == 1
    assert encode_parity(" Even ") == 2


def test_encode_parity_rejects_unknown():
    with pytest.raises(ValueError):
        encode_parity("space")


def test_validate_slave_id_range():
    assert validate_slave_id(1) == 1
    assert validate_slave_id(255) == 255
    with pytest.raises(ValueError):
        validate_slave_id(0)
    with pytest.raises(ValueError):
        validate_slave_id(256)
    with pytest.raises(ValueError):
        validate_slave_id("1")  # type: ignore[arg-type]
