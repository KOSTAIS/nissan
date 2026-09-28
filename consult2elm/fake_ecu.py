"""A simulated Nissan ECU that behaves like a pyserial port.

Used by the tests and by `--simulate`, so the whole chain (CONSULT ->
ELM327 -> Bluetooth/TCP) can be tried on a desk without the car.
"""

from __future__ import annotations

import math
import threading
import time

from .consult import (
    CMD_READ_REGISTER,
    CMD_START_STREAM,
    CMD_STOP,
    FRAME_START,
    INIT_ACK,
    REG_BATTERY,
    REG_COOLANT,
    REG_RPM_LSB,
    REG_RPM_MSB,
    REG_SPEED,
)


class FakeConsultECU:
    def __init__(self, ignition: bool = True, engine_running: bool = True, frame_interval: float = 0.02):
        self.timeout = 0.1
        self.frame_interval = frame_interval
        self.engine_running = engine_running
        self.rpm = 850.0
        self.coolant_c = 85
        self.speed_kmh = 0
        self.animate = False  # vary the values over time (for --simulate)
        self._cond = threading.Condition()
        self._rx = bytearray()      # bytes waiting to be read by the host
        self._cmd = bytearray()     # partial command received from the host
        self._registers = []
        self._initialised = False
        self._streaming = False
        self._next_frame = 0.0
        self._t0 = time.monotonic()
        self._ignition = ignition

    # -- test controls -----------------------------------------------------------

    @property
    def ignition(self) -> bool:
        return self._ignition

    @ignition.setter
    def ignition(self, on: bool) -> None:
        with self._cond:
            self._ignition = on
            if not on:  # ECU loses power and forgets everything
                self._initialised = self._streaming = False
                self._registers = []
                self._rx.clear()
                self._cmd.clear()

    # -- pyserial-like interface ---------------------------------------------------

    def write(self, data: bytes) -> int:
        with self._cond:
            if self._ignition:
                for byte in data:
                    self._receive(byte)
            self._cond.notify_all()
        return len(data)

    def read(self, size: int = 1) -> bytes:
        deadline = time.monotonic() + self.timeout
        with self._cond:
            while True:
                self._produce_frames()
                if self._rx:
                    out = bytes(self._rx[:size])
                    del self._rx[:size]
                    return out
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cond.wait(min(remaining, self.frame_interval))

    @property
    def in_waiting(self) -> int:
        with self._cond:
            self._produce_frames()
            return len(self._rx)

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._rx.clear()

    def close(self) -> None:
        pass

    # -- ECU behaviour -----------------------------------------------------------------

    def _receive(self, byte: int) -> None:
        if self._streaming:
            if byte == CMD_STOP:
                self._streaming = False
                self._registers = []
                self._rx.append(0xCF)
            return
        self._cmd.append(byte)
        if self._cmd[-3:] == b"\xFF\xFF\xEF":
            self._initialised = True
            self._registers = []
            self._rx.append(INIT_ACK)
            self._cmd.clear()
            return
        if not self._initialised:
            del self._cmd[:-2]
            return
        first = self._cmd[0]
        if first == CMD_READ_REGISTER:
            if len(self._cmd) == 2:
                self._registers.append(byte)
                self._rx += bytes((0xA5, byte))
                self._cmd.clear()
        elif first == CMD_START_STREAM:
            self._cmd.clear()
            if self._registers:
                self._streaming = True
                self._next_frame = time.monotonic()
        elif first == CMD_STOP:
            self._cmd.clear()
            self._registers = []
            self._rx.append(0xCF)
        elif first != 0xFF or len(self._cmd) >= 3:
            self._cmd.clear()

    def _produce_frames(self) -> None:
        if not self._streaming:
            return
        now = time.monotonic()
        produced = 0
        while self._next_frame <= now and produced < 5:
            values = self._register_values()
            self._rx += bytes((FRAME_START, len(self._registers)))
            self._rx += bytes(values.get(r, 0) for r in self._registers)
            self._next_frame += self.frame_interval
            produced += 1
        if self._next_frame < now:
            self._next_frame = now

    def _register_values(self) -> dict:
        rpm, speed, coolant = self.rpm, self.speed_kmh, self.coolant_c
        if self.animate:
            t = time.monotonic() - self._t0
            rpm = 850 + 1500 * (1 + math.sin(t / 7)) / 2
            speed = int(60 * (1 + math.sin(t / 11)) / 2)
            coolant = min(90, 20 + int(t / 2))
        if not self.engine_running:
            rpm, speed = 0.0, 0
        raw_rpm = int(round(rpm / 12.5))
        battery = 14.2 if self.engine_running else 12.5
        return {
            REG_RPM_MSB: (raw_rpm >> 8) & 0xFF,
            REG_RPM_LSB: raw_rpm & 0xFF,
            REG_COOLANT: max(0, min(255, coolant + 50)),
            REG_SPEED: max(0, min(255, speed // 2)),
            REG_BATTERY: int(round(battery / 0.08)),
        }
