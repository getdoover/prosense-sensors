"""Simulator that emits ProSense-shaped raw register values via tags.

The real app, when ``sim_app_key`` is configured, reads these tags
instead of hitting the Modbus bus. Values mirror the format the sensor
itself returns over Modbus:

    sim_unit_code        uint16  unit code at register 0x0002 (20 = °C)
    sim_decimal_point    uint16  decimal-point position at register 0x0003
    sim_pv_int16         int16   raw PV at register 0x0004
                                 (decimal_point applied → physical value)
    sim_pv_float_msw     uint16  high word of IEEE754 float at 0x0016
    sim_pv_float_lsw     uint16  low word of IEEE754 float at 0x0017
"""

import math
import random
import struct

from pydoover.docker import Application, run_app
from pydoover.tags import Tag, Tags


UNIT_CELSIUS = 20
DECIMAL_POINT = 1   # one decimal place, e.g. 23.4 °C → int16 234


class ProSenseSimulatorTags(Tags):
    sim_unit_code = Tag("integer", default=UNIT_CELSIUS)
    sim_decimal_point = Tag("integer", default=DECIMAL_POINT)
    sim_pv_int16 = Tag("integer", default=0)
    sim_pv_float_msw = Tag("integer", default=0)
    sim_pv_float_lsw = Tag("integer", default=0)


class ProSenseSimulator(Application):
    tags_cls = ProSenseSimulatorTags
    loop_target_period = 1

    async def setup(self):
        # Seed a meandering temperature so values don't look like pure noise.
        self._temperature_c = random.uniform(18.0, 25.0)
        self._tick = 0

    async def main_loop(self):
        self._tick += 1

        # Slow random walk + a slow sinusoid pulled toward 22 °C, clipped
        # to a plausible indoor/outdoor range.
        drift = random.gauss(0.0, 0.05)
        sinus = 0.5 * math.sin(self._tick / 30.0)
        self._temperature_c += drift + 0.02 * (22.0 - self._temperature_c) + 0.01 * sinus
        self._temperature_c = max(-40.0, min(125.0, self._temperature_c))

        # int16 + decimal-point representation (reg 0x0004 + 0x0003).
        scaled = round(self._temperature_c * (10 ** DECIMAL_POINT))
        scaled = max(-32768, min(32767, int(scaled)))
        raw_int16 = scaled & 0xFFFF  # two's-complement uint16 wire encoding

        # IEEE754 float ABCD representation (regs 0x0016 / 0x0017).
        packed = struct.pack(">f", float(self._temperature_c))
        msw, lsw = struct.unpack(">HH", packed)

        await self.tags.sim_unit_code.set(UNIT_CELSIUS)
        await self.tags.sim_decimal_point.set(DECIMAL_POINT)
        await self.tags.sim_pv_int16.set(raw_int16)
        await self.tags.sim_pv_float_msw.set(int(msw))
        await self.tags.sim_pv_float_lsw.set(int(lsw))


def main():
    """Run the ProSense simulator application."""
    run_app(ProSenseSimulator())


if __name__ == "__main__":
    main()
