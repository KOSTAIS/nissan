"""ELM327 command interpreter.

Answers the AT commands an OBD client (here: the Teltonika FMB130) sends to
set up an ELM327 adapter, and translates OBD-II mode 01 requests into values
taken from the DataStore.

The emulated vehicle protocol is ISO 9141-2 (protocol 3), which is what a
real OBD-II car of this age would use, so headers (ATH1) look authentic.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

from .data import DataStore, EngineData

log = logging.getLogger(__name__)

ELM_VERSION = "ELM327 v1.5"
ELM_DESCRIPTION = "OBDII to RS232 Interpreter"
PROTOCOL_NAME = "ISO 9141-2"
PROTOCOL_NUMBER = "3"
ISO9141_HEADER = bytes((0x48, 0x6B, 0x10))  # priority, target, ECU address

PID_MONITOR_STATUS = 0x01
PID_COOLANT_TEMP = 0x05
PID_RPM = 0x0C
PID_SPEED = 0x0D
SUPPORTED_PIDS = (PID_MONITOR_STATUS, PID_COOLANT_TEMP, PID_RPM, PID_SPEED)

_FLAG_COMMAND = re.compile(r"^([ELSH])([01])$")
_HEX = re.compile(r"^[0-9A-F]+$")
_MAX_LINE = 64


def _clamp(value: float, low: int, high: int) -> int:
    return max(low, min(high, int(round(value))))


def supported_pids_mask(base: int, supported=SUPPORTED_PIDS) -> int:
    """Bit mask for PID base+1 .. base+0x20 (MSB = base+1), as in PID 00."""
    mask = 0
    for pid in supported:
        if base < pid <= base + 0x20:
            mask |= 1 << (0x20 - (pid - base))
    if any(pid > base + 0x20 for pid in supported):
        mask |= 1  # "next range supported" bit
    return mask


class Elm327:
    def __init__(self, store: DataStore, vin: str = ""):
        self._store = store
        # A 1997 ECU has no VIN; one can be configured so the tracker reports it.
        self._vin = vin.strip().upper()[:17].rjust(17, "0") if vin.strip() else ""
        self._line = bytearray()
        self._last_command = ""
        self.reset()

    def reset(self) -> None:
        self.echo = True
        self.linefeeds = False
        self.spaces = True
        self.headers = False
        self.auto_protocol = True

    # -- byte stream interface -------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        """Consume bytes from the client, return bytes to send back."""
        out = bytearray()
        for byte in data:
            if byte == 0x0D:
                out += self._complete_line()
            elif byte in (0x0A, 0x00):
                continue
            elif len(self._line) < _MAX_LINE:
                self._line.append(byte)
        return bytes(out)

    def _complete_line(self) -> bytes:
        raw = self._line.decode("ascii", "ignore")
        self._line.clear()
        command = raw.replace(" ", "").upper()
        if command:
            self._last_command = command
        else:
            command = self._last_command  # empty line repeats the last command

        echoed = raw + "\r" if self.echo else ""
        lines = self.handle(command) if command else []
        eol = "\r\n" if self.linefeeds else "\r"
        body = "".join(line + eol for line in lines)
        return (echoed + body + eol + ">").encode("ascii")

    # -- command handling --------------------------------------------------------

    def handle(self, command: str) -> List[str]:
        if command.startswith("AT"):
            return self._at_command(command[2:])
        if _HEX.match(command) and len(command) >= 2:
            return self._obd_request(command)
        return ["?"]

    def _at_command(self, cmd: str) -> List[str]:
        flag = _FLAG_COMMAND.match(cmd)
        if flag:
            attr = {"E": "echo", "L": "linefeeds", "S": "spaces", "H": "headers"}[flag.group(1)]
            setattr(self, attr, flag.group(2) == "1")
            return ["OK"]
        if cmd in ("Z", "WS"):
            self.reset()
            return ["", ELM_VERSION]
        if cmd == "D":
            self.reset()
            return ["OK"]
        if cmd == "I":
            return [ELM_VERSION]
        if cmd == "@1":
            return [ELM_DESCRIPTION]
        if cmd == "RV":
            data = self._store.current() or self._store.last_known()
            return [f"{data.battery_v:.1f}V"] if data else ["?"]
        if cmd == "IGN":
            return ["ON" if self._store.current() else "OFF"]
        if cmd == "DP":
            return [("AUTO, " if self.auto_protocol else "") + PROTOCOL_NAME]
        if cmd == "DPN":
            return [("A" if self.auto_protocol else "") + PROTOCOL_NUMBER]
        if cmd.startswith(("SP", "TP")):
            arg = cmd[2:]
            self.auto_protocol = arg.startswith("A") or arg == "0"
            return ["OK"]
        # Timing, CAN filters, set header, memory, low power, ...: nothing to
        # configure on a fake bus, but clients expect them to succeed.
        log.debug("Accepting AT%s without action", cmd)
        return ["OK"]

    def _obd_request(self, command: str) -> List[str]:
        if len(command) % 2:
            command = command[:-1]  # trailing digit = expected response count
        request = bytes.fromhex(command)
        mode, pids = request[0], request[1:]
        engine = self._store.current()

        messages: List[bytes] = []
        if mode == 0x01:
            for pid in pids:
                payload = self._mode01(pid, engine)
                if payload is not None:
                    messages.append(bytes((0x41, pid)) + payload)
        elif mode in (0x03, 0x07) and engine is not None:
            messages.append(bytes((mode + 0x40,)) + bytes(6))  # no trouble codes
        elif mode == 0x04 and engine is not None:
            messages.append(b"\x44")
        elif mode == 0x09 and self._vin and len(pids) == 1:
            messages.extend(self._mode09(pids[0]))
        return [self._format(m) for m in messages] or ["NO DATA"]

    def _mode01(self, pid: int, engine: Optional[EngineData]) -> Optional[bytes]:
        if pid % 0x20 == 0:
            # Supported-PID bitmaps are static; answer 0100 even with the
            # engine off so the client keeps the adapter as "connected".
            if pid == 0 or supported_pids_mask(pid - 0x20) & 1:
                return supported_pids_mask(pid).to_bytes(4, "big")
            return None
        if engine is None:
            return None
        if pid == PID_MONITOR_STATUS:
            return bytes(4)  # MIL off, 0 DTCs, no readiness tests
        if pid == PID_COOLANT_TEMP:
            return bytes((_clamp(engine.coolant_c + 40, 0, 255),))
        if pid == PID_RPM:
            return _clamp(engine.rpm * 4, 0, 0xFFFF).to_bytes(2, "big")
        if pid == PID_SPEED:
            return bytes((_clamp(engine.speed_kmh, 0, 255),))
        return None

    def _mode09(self, pid: int) -> List[bytes]:
        if pid == 0x00:
            return [bytes((0x49, 0x00, 0x40, 0x00, 0x00, 0x00))]  # only PID 02 (VIN)
        if pid == 0x02:
            # ISO 9141-2 style: 5 frames "49 02 NN" + 4 bytes, VIN padded with 3 leading zeros.
            data = bytes(3) + self._vin.encode("ascii", "replace")
            return [bytes((0x49, 0x02, n + 1)) + data[n * 4:n * 4 + 4] for n in range(5)]
        return []

    def _format(self, message: bytes) -> str:
        if self.headers:
            framed = ISO9141_HEADER + message
            message = framed + bytes((sum(framed) & 0xFF,))
        sep = " " if self.spaces else ""
        return sep.join(f"{b:02X}" for b in message)
