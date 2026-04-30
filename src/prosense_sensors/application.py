import logging
import time

from pydoover.docker import Application

from .app_config import ProSenseConfig
from .app_tags import ProSenseTags
from .app_ui import ProSenseUI
from .prosense_driver import (
    META_BLOCK_COUNT,
    META_BLOCK_START,
    PV_INT16_BLOCK_COUNT,
    PV_INT16_BLOCK_START,
    REG_BAUD,
    REG_PARITY,
    REG_SLAVE_ID,
    MetaReading,
    TemperatureUnit,
    Variant,
    convert_temperature,
    decode_meta,
    decode_pv_int16,
    encode_baud,
    encode_parity,
    temperature_unit_from_code,
    to_signed16,
    unit_label,
    validate_slave_id,
)

log = logging.getLogger(__name__)

MODBUS_HOLDING_REGISTER = 4  # pydoover register-type code for holding registers

# Simulator tag names — matched by simulators/sample/main.py.
SIM_TAG_UNIT_CODE = "sim_unit_code"
SIM_TAG_DECIMAL_POINT = "sim_decimal_point"
SIM_TAG_PV_INT16 = "sim_pv_int16"


class ProSenseApplication(Application):
    config_cls = ProSenseConfig
    tags_cls = ProSenseTags
    ui_cls = ProSenseUI

    config: ProSenseConfig
    tags: ProSenseTags

    # Loop fires every second; we throttle the actual Modbus poll to
    # ``config.poll_interval_seconds`` so command-tag handling stays
    # responsive between polls.
    loop_target_period = 1

    async def setup(self):
        self._last_poll_ts: float = 0.0
        self._last_successful_read_ts: float = 0.0
        self._warned_unit_codes: set[int] = set()

    async def main_loop(self):
        now = time.time()
        await self._handle_command_tags()

        if now - self._last_poll_ts >= self.config.poll_interval_seconds.value:
            self._last_poll_ts = now
            await self._poll(now)

    # -----------------------------------------------------------------
    # Polling
    # -----------------------------------------------------------------

    async def _poll(self, now: float) -> None:
        # The IEEE754 float register at 0x0016 isn't exposed by every
        # ProSense probe (e.g. the SBWR-LED temperature variant skips
        # it), and probing for it on every poll produces a torrent of
        # Modbus exceptions. We read the int16 PV at 0x0004 + decimal
        # point at 0x0003 instead — present on the whole product family.
        meta_regs = await self._read_meta()
        meta = decode_meta(meta_regs)

        pv_int16_regs = await self._read_pv_int16()
        pv_int16_decoded = decode_pv_int16(pv_int16_regs, meta.decimal_point)

        await self._publish(
            now=now,
            meta=meta,
            pv_int16_regs=pv_int16_regs,
            pv_int16_decoded=pv_int16_decoded,
        )

    async def _read_meta(self):
        if self._sim_enabled():
            return self._read_meta_from_sim()
        return await self._modbus_read(META_BLOCK_START, META_BLOCK_COUNT, "meta")

    async def _read_pv_int16(self):
        if self._sim_enabled():
            return self._read_pv_int16_from_sim()
        return await self._modbus_read(PV_INT16_BLOCK_START, PV_INT16_BLOCK_COUNT, "pv_int16")

    async def _modbus_read(self, start: int, count: int, label: str):
        try:
            result = await self.modbus_iface.read_registers(
                bus_id=self.config.modbus_config.name.value,
                modbus_id=self.config.slave_id.value,
                start_address=start,
                num_registers=count,
                register_type=MODBUS_HOLDING_REGISTER,
            )
        except Exception:
            log.exception("Modbus read failed (%s @ 0x%04X x %d)", label, start, count)
            return None
        if result is None:
            return None
        # pydoover returns a bare int when num_registers=1; normalise to a list.
        return [result] if isinstance(result, int) else result

    # ---- sim path ---------------------------------------------------

    def _sim_enabled(self) -> bool:
        return bool(self.config.sim_app_key.value)

    def _sim_key(self) -> str:
        return self.config.sim_app_key.value

    def _read_meta_from_sim(self):
        unit = self.get_tag(SIM_TAG_UNIT_CODE, self._sim_key())
        dp = self.get_tag(SIM_TAG_DECIMAL_POINT, self._sim_key())
        if unit is None or dp is None:
            return None
        return [int(unit), int(dp)]

    def _read_pv_int16_from_sim(self):
        raw = self.get_tag(SIM_TAG_PV_INT16, self._sim_key())
        if raw is None:
            return None
        return [int(raw)]

    # -----------------------------------------------------------------
    # Publishing
    # -----------------------------------------------------------------

    async def _publish(
        self,
        *,
        now: float,
        meta: MetaReading,
        pv_int16_regs,
        pv_int16_decoded: float | None,
    ) -> None:
        # Diagnostics — publish whatever we managed to read, even on
        # partial failure, so a technician can see what's coming back.
        if meta.unit_code is not None:
            await self.tags.sensor_unit_code.set(meta.unit_code)
            await self.tags.sensor_unit_label.set(unit_label(meta.unit_code))
        if meta.decimal_point is not None:
            await self.tags.decimal_point.set(meta.decimal_point)

        if pv_int16_regs is not None and len(pv_int16_regs) >= 1:
            await self.tags.raw_pv_int16.set(to_signed16(int(pv_int16_regs[0])))

        if pv_int16_decoded is None:
            await self._note_failed_read(now)
            return

        await self.tags.pv_native.set(round(pv_int16_decoded, 4))

        published = self._convert_for_display(pv_int16_decoded, meta)
        if published is None:
            # We got a number from the sensor but can't safely convert
            # it — e.g. temperature variant but the sensor is reporting
            # in something other than °C/°F. Mark as no-comms-progress
            # so the operator notices.
            await self._note_failed_read(now)
            return

        await self.tags.temperature.set(round(published, 2))
        await self.tags.last_read_time.set(now)
        await self.tags.comms_ok.set(True)
        await self.tags.temperature_ok.set(self._is_temperature_ok(published))
        self._last_successful_read_ts = now

    def _is_temperature_ok(self, value: float) -> bool:
        """True when temperature is below the over-temp threshold (or none set)."""
        threshold = self.config.over_temp_threshold.value
        if threshold is None:
            return True
        return value < threshold

    def _convert_for_display(self, value_native: float, meta: MetaReading) -> float | None:
        variant = Variant(self.config.variant.value)

        if variant is not Variant.TEMPERATURE:
            # Other variants aren't fully wired up yet; pass the native
            # value through so they at least report *something*.
            log.debug("Variant %s not fully implemented; publishing native value", variant)
            return value_native

        # Temperature path: we need to know the sensor's native unit
        # (°C or °F) to convert into the user's display unit. Different
        # ProSense temperature probes use 0x0002 for sub-type / model
        # codes that don't follow the pressure-family unit table, so an
        # unrecognised code isn't a publish-blocker — the user has
        # already declared the variant. Treat unknown codes as °C and
        # warn once per code.
        sensor_unit = temperature_unit_from_code(meta.unit_code)
        if sensor_unit is None:
            if meta.unit_code is not None and meta.unit_code not in self._warned_unit_codes:
                log.warning(
                    "Sensor unit code %s (%s) is not a known temperature unit; "
                    "assuming °C (variant=temperature is set in config)",
                    meta.unit_code, unit_label(meta.unit_code),
                )
                self._warned_unit_codes.add(meta.unit_code)
            sensor_unit = TemperatureUnit.CELSIUS

        display = TemperatureUnit(self.config.display_unit.value)
        return convert_temperature(value_native, sensor_unit, display)

    async def _note_failed_read(self, now: float) -> None:
        # No usable reading this cycle: drop the temperature to null
        # immediately so the UI stops showing a stale value. The
        # comms_ok flag has a grace window so a single missed read
        # doesn't flap the indicator.
        await self.tags.temperature.set(None)
        staleness = now - self._last_successful_read_ts
        timeout = self.config.no_comms_timeout_seconds.value
        if self._last_successful_read_ts == 0 or staleness > timeout:
            await self.tags.comms_ok.set(False)

    # -----------------------------------------------------------------
    # Technician command tags
    # -----------------------------------------------------------------

    async def _handle_command_tags(self) -> None:
        if self._sim_enabled():
            # Refuse loudly so technicians don't think they reconfigured
            # a real sensor that isn't actually on the bus.
            for tag_name in ("cmd_set_slave_id", "cmd_set_baud", "cmd_set_parity"):
                if self.tags.get(tag_name).get() is not None:
                    await self._finalise_cmd(tag_name, "error: simulator mode, write ignored")
            return

        slave_cmd = self.tags.cmd_set_slave_id.get()
        if slave_cmd is not None:
            await self._write_commissioning(
                "cmd_set_slave_id", REG_SLAVE_ID,
                encode_fn=validate_slave_id, value=slave_cmd,
            )

        baud_cmd = self.tags.cmd_set_baud.get()
        if baud_cmd is not None:
            await self._write_commissioning(
                "cmd_set_baud", REG_BAUD,
                encode_fn=encode_baud, value=baud_cmd,
            )

        parity_cmd = self.tags.cmd_set_parity.get()
        if parity_cmd is not None:
            await self._write_commissioning(
                "cmd_set_parity", REG_PARITY,
                encode_fn=encode_parity, value=parity_cmd,
            )

    async def _write_commissioning(self, tag_name: str, register: int, *, encode_fn, value) -> None:
        try:
            encoded = encode_fn(value)
        except ValueError as exc:
            await self._finalise_cmd(tag_name, f"error: {exc}")
            return

        try:
            await self.modbus_iface.write_registers(
                bus_id=self.config.modbus_config.name.value,
                modbus_id=self.config.slave_id.value,
                start_address=register,
                values=[encoded],
                register_type=MODBUS_HOLDING_REGISTER,
            )
        except Exception as exc:
            log.exception("Modbus write failed for %s", tag_name)
            await self._finalise_cmd(tag_name, f"error: {exc}")
            return

        await self._finalise_cmd(
            tag_name,
            f"ok: wrote {encoded} to register 0x{register:04X} (power-cycle sensor to apply)",
        )

    async def _finalise_cmd(self, tag_name: str, result: str) -> None:
        await self.tags.last_cmd_result.set(result)
        await self.tags.get(tag_name).set(None)
        log.info("Command %s → %s", tag_name, result)
