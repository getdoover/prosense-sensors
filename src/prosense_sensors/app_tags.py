from pydoover.tags import Tag, Tags


class ProSenseTags(Tags):
    # ---- live reading (in the configured display_unit) ------------------
    # default=None so the UI shows "—" before the first read and after
    # the no-comms timeout, rather than a misleading 0.
    temperature = Tag("number", default=None)

    # ---- diagnostics / raw values ---------------------------------------
    # Sensor-side unit code from register 0x0002 (e.g. 20 = °C, 22 = °F).
    sensor_unit_code = Tag("integer", default=0)
    sensor_unit_label = Tag("string", default="")
    # Decimal-point position from register 0x0003.
    decimal_point = Tag("integer", default=0)
    # Raw int16 PV from register 0x0004 (sign-extended).
    raw_pv_int16 = Tag("integer", default=0)
    # PV decoded from int16+decimal_point, in the sensor's native unit
    # (before display-unit conversion). Useful for spotting a unit
    # mismatch between sensor config and app config.
    pv_native = Tag("number", default=0)

    # ---- comms ----------------------------------------------------------
    comms_ok = Tag("boolean", default=False)
    last_read_time = Tag("number", default=0)

    # ---- alarm state ---------------------------------------------------
    # ``temperature_ok`` is true when the current temperature is below
    # ``over_temp_threshold`` (or when no threshold is configured). The
    # over-temperature WarningIndicator's ``hidden`` is bound directly to
    # this tag so it auto-shows when the temperature crosses the threshold.
    temperature_ok = Tag("boolean", default=True)

    # ---- technician command tags (no UI; tag-only by design) -----------
    # Set one of these to a non-null value to trigger a Modbus write.
    # The app clears the tag after executing and writes a result string
    # to last_cmd_result. default=None so `is not None` filters out unset.
    cmd_set_slave_id = Tag("integer", default=None)
    cmd_set_baud = Tag("integer", default=None)
    cmd_set_parity = Tag("string", default=None)
    last_cmd_result = Tag("string", default="")
