#!/usr/bin/env python3
"""Commissioning tool: set a ProSense (H-Link) sensor's Modbus address + baud.

Out of the box every ProSense sensor ships on slave id 1 @ 9600 8N1. On a
bus with more than one sensor that's a collision, so during commissioning
each unit is given a unique address (and optionally a faster baud) one at a
time — connect a single sensor, run this, power-cycle it, move to the next.

    # give the connected sensor address 10, keep 9600 baud
    python scripts/commission_sensor.py --new-id 10

    # address 11 and bump it to 19200 baud
    python scripts/commission_sensor.py --new-id 11 --new-baud 19200

    # talk to a sensor that's NOT on the defaults (e.g. re-addressing)
    python scripts/commission_sensor.py --from-id 10 --from-baud 19200 --new-id 12

    # write, then re-open at the new settings and read the registers back
    python scripts/commission_sensor.py --new-id 10 --new-baud 19200 --verify

Changes are saved to the sensor's user area and take effect on the next
power cycle (per the H-Link manual / the app's commissioning path). The
sensor keeps answering on its OLD address/baud until it is restarted, so
--verify power-cycles nothing — it just sanity-checks the registers were
accepted by re-reading at the new settings, which only succeeds on probes
that apply changes live. Treat a --verify miss as "power-cycle and check",
not "the write failed".

Only dependency is pyserial (`pip install pyserial`); the Modbus RTU
framing is done here so there's no pymodbus version dance. Register map and
the baud/slave-id encoders are imported from the app driver so this tool
can never drift from what the running app expects.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Reuse the app's register map + encoders rather than re-deriving them. We
# import the bare driver module file directly (adding its dir to the path)
# rather than `prosense_sensors.prosense_driver` — the latter would run the
# package __init__, which pulls in pydoover and the whole app. prosense_driver
# only needs the stdlib, so a commissioning tool can use it with nothing else
# installed. Candidates: the in-repo package dir, and the script's own dir
# (the flat copy made by the remote-run wrapper).
_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE.parent / "src" / "prosense_sensors", _HERE):
    if _candidate.is_dir():
        sys.path.insert(0, str(_candidate))

from prosense_driver import (  # type: ignore  # noqa: E402
    REG_BAUD, REG_SAVE_USER, REG_SLAVE_ID,
    encode_baud, validate_slave_id, _BAUD_CHOICES,
)

FC_READ_HOLDING = 0x03
FC_WRITE_SINGLE = 0x06
SAVE_USER_MAGIC = 0  # write 0 to REG_SAVE_USER → persist user area


# ---- Modbus RTU framing ------------------------------------------------

def crc16(data: bytes) -> int:
    """Standard Modbus RTU CRC-16 (poly 0xA001), returned host-order."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _frame(payload: bytes) -> bytes:
    crc = crc16(payload)
    return payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


class ModbusError(RuntimeError):
    pass


class RtuClient:
    """Minimal Modbus RTU master over a serial line — FC03 read, FC06 write."""

    def __init__(self, port: str, baud: int, parity: str, timeout: float):
        try:
            import serial  # pyserial — imported lazily so --help works without it
        except ModuleNotFoundError:
            sys.exit("pyserial is required: `pip install pyserial` "
                     "(or `uv pip install pyserial`).")
        self._ser = serial.Serial(
            port=port,
            baudrate=baud,
            parity={"none": serial.PARITY_NONE,
                    "even": serial.PARITY_EVEN,
                    "odd": serial.PARITY_ODD}[parity],
            stopbits=serial.STOPBITS_ONE,
            bytesize=serial.EIGHTBITS,
            timeout=timeout,
        )

    def close(self) -> None:
        self._ser.close()

    def __enter__(self) -> "RtuClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _txn(self, slave: int, payload: bytes, expected_len: int) -> bytes:
        request = _frame(bytes([slave]) + payload)
        self._ser.reset_input_buffer()
        self._ser.write(request)
        self._ser.flush()
        resp = self._ser.read(expected_len)
        if len(resp) < 3:
            raise ModbusError(
                f"no/short response from slave {slave} "
                f"(got {len(resp)} bytes — wrong address, baud, or wiring?)"
            )
        if crc16(resp[:-2]) != (resp[-2] | (resp[-1] << 8)):
            raise ModbusError(f"bad CRC in response from slave {slave}: {resp.hex()}")
        if resp[1] & 0x80:
            code = resp[2] if len(resp) > 2 else -1
            raise ModbusError(f"slave {slave} returned Modbus exception 0x{code:02X}")
        return resp

    def read_holding(self, slave: int, start: int, count: int) -> list[int]:
        payload = bytes([FC_READ_HOLDING,
                         (start >> 8) & 0xFF, start & 0xFF,
                         (count >> 8) & 0xFF, count & 0xFF])
        # addr(1)+fc(1)+bytecount(1)+data(2*count)+crc(2)
        resp = self._txn(slave, payload, 5 + 2 * count)
        byte_count = resp[2]
        data = resp[3:3 + byte_count]
        if len(data) < 2 * count:
            raise ModbusError(f"short register payload from slave {slave}: {resp.hex()}")
        return [(data[i] << 8) | data[i + 1] for i in range(0, 2 * count, 2)]

    def write_single(self, slave: int, register: int, value: int) -> None:
        payload = bytes([FC_WRITE_SINGLE,
                         (register >> 8) & 0xFF, register & 0xFF,
                         (value >> 8) & 0xFF, value & 0xFF])
        # FC06 response echoes the 8-byte request.
        resp = self._txn(slave, payload, 8)
        echoed_reg = (resp[2] << 8) | resp[3]
        echoed_val = (resp[4] << 8) | resp[5]
        if echoed_reg != register or echoed_val != value:
            raise ModbusError(
                f"slave {slave} echo mismatch on write to 0x{register:04X}: "
                f"sent {value}, echoed reg=0x{echoed_reg:04X} val={echoed_val}"
            )


# ---- commissioning flow ------------------------------------------------

def commission(args: argparse.Namespace) -> int:
    new_id = validate_slave_id(args.new_id)
    new_baud_code = encode_baud(args.new_baud) if args.new_baud else None

    print(f"Opening {args.port} @ {args.from_baud} {args.parity[0].upper()}81 "
          f"(reaching sensor at slave id {args.from_id})")

    with RtuClient(args.port, args.from_baud, args.parity, args.timeout) as client:
        # 1. Confirm we can actually talk to the sensor before changing anything.
        try:
            current = client.read_holding(args.from_id, REG_SLAVE_ID, 2)
        except ModbusError as exc:
            print(f"  ✗ can't reach sensor: {exc}", file=sys.stderr)
            return 2
        cur_id, cur_baud_code = current[0], current[1]
        cur_baud = _baud_from_code(cur_baud_code)
        print(f"  ✓ found sensor: slave id {cur_id}, baud code {cur_baud_code} "
              f"({cur_baud or '?'} baud)")

        if args.dry_run:
            print("  (dry run — no writes performed)")
            _print_plan(new_id, args.new_baud, new_baud_code)
            return 0

        # 2. Write baud first, then id. Order matters only if the sensor
        #    applied changes live (it doesn't — they apply on restart), but
        #    writing id last keeps the "we addressed the request to from_id"
        #    invariant true for every write in the session.
        if new_baud_code is not None:
            print(f"  → set baud to {args.new_baud} (code {new_baud_code}) "
                  f"@ reg 0x{REG_BAUD:04X}")
            client.write_single(args.from_id, REG_BAUD, new_baud_code)

        print(f"  → set slave id to {new_id} @ reg 0x{REG_SLAVE_ID:04X}")
        client.write_single(args.from_id, REG_SLAVE_ID, new_id)

        # 3. Persist to the user area so it survives the power cycle.
        if not args.no_save:
            print(f"  → save user area (write {SAVE_USER_MAGIC} @ reg 0x{REG_SAVE_USER:04X})")
            try:
                client.write_single(args.from_id, REG_SAVE_USER, SAVE_USER_MAGIC)
            except ModbusError as exc:
                # Some probes auto-persist FC06 writes and NAK the save reg.
                print(f"    (save register not accepted: {exc} — likely auto-persisted)")

    print("  ✓ writes accepted (sensor echoed every write)")
    print(f"\nPower-cycle the sensor to apply: it will come up as slave id {new_id}"
          + (f" @ {args.new_baud} baud" if args.new_baud else "") + ".")

    if args.verify:
        _verify(args, new_id)

    return 0


def _verify(args: argparse.Namespace, new_id: int) -> None:
    verify_baud = args.new_baud or args.from_baud
    print(f"\nVerifying: re-opening @ {verify_baud} baud, reading slave id {new_id} ...")
    if args.verify_delay:
        time.sleep(args.verify_delay)
    try:
        with RtuClient(args.port, verify_baud, args.parity, args.timeout) as client:
            regs = client.read_holding(new_id, REG_SLAVE_ID, 2)
    except (ModbusError, OSError) as exc:
        print(f"  · no answer at the new settings ({exc}).")
        print("    This is expected on probes that only apply changes after a "
              "power cycle. Restart the sensor and re-run with --from-id "
              f"{new_id}" + (f" --from-baud {verify_baud}" if args.new_baud else "")
              + " to confirm.")
        return
    print(f"  ✓ sensor now reports slave id {regs[0]}, baud code {regs[1]} "
          f"({_baud_from_code(regs[1]) or '?'} baud)")


def _baud_from_code(code: int) -> int | None:
    for baud, c in _BAUD_CHOICES.items():
        if c == code:
            return baud
    return None


def _print_plan(new_id: int, new_baud: int | None, new_baud_code: int | None) -> None:
    print(f"  would set slave id → {new_id} (reg 0x{REG_SLAVE_ID:04X})")
    if new_baud_code is not None:
        print(f"  would set baud → {new_baud} / code {new_baud_code} (reg 0x{REG_BAUD:04X})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Set a ProSense sensor's Modbus slave id and baud rate.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--port", default="/dev/ttyAMA0", help="serial device")
    p.add_argument("--new-id", type=int, required=True,
                   help="new Modbus slave id to assign (1..255)")
    p.add_argument("--new-baud", type=int, default=None, choices=sorted(_BAUD_CHOICES),
                   help="new baud rate (leave unset to keep the current baud)")
    p.add_argument("--from-id", type=int, default=1,
                   help="slave id the sensor currently answers on")
    p.add_argument("--from-baud", type=int, default=9600, choices=sorted(_BAUD_CHOICES),
                   help="baud the sensor currently uses")
    p.add_argument("--parity", default="none", choices=("none", "even", "odd"),
                   help="serial parity (sensors ship 8N1)")
    p.add_argument("--timeout", type=float, default=1.0, help="per-response timeout (s)")
    p.add_argument("--no-save", action="store_true",
                   help="don't write the save-user-area register")
    p.add_argument("--verify", action="store_true",
                   help="re-open at the new settings and read the registers back")
    p.add_argument("--verify-delay", type=float, default=0.0,
                   help="seconds to wait before --verify (give a live-apply sensor time)")
    p.add_argument("--dry-run", action="store_true",
                   help="connect and report, but write nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return commission(args)
    except (ValueError, ModbusError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        # pyserial's SerialException subclasses OSError; also covers
        # "no such port" / permission-denied on the device node.
        print(f"serial error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
