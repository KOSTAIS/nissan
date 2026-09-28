"""Nissan CONSULT (Consult-I) protocol client.

Protocol summary (9600 baud, 8N1):

  * Initialise:      host sends FF FF EF, ECU answers 10.
  * Register stream: host sends 5A <reg> for every register, then F0.
                     ECU echoes A5 <reg> for each, then streams frames
                     FF <n> <byte for reg 1> ... <byte for reg n> forever.
  * Stop:            host sends 30, ECU answers CF.

Registers used here (ECCS engine ECU):

  0x00/0x01  engine speed (CAS position) MSB/LSB   rpm  = value * 12.5
  0x08       coolant temperature                    degC = value - 50
  0x0B       vehicle speed                          km/h = value * 2
  0x0C       battery voltage                        V    = value * 0.080
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable, List, Optional

from .data import DataStore, EngineData

log = logging.getLogger(__name__)

REG_RPM_MSB = 0x00
REG_RPM_LSB = 0x01
REG_COOLANT = 0x08
REG_SPEED = 0x0B
REG_BATTERY = 0x0C
STREAM_REGISTERS = (REG_RPM_MSB, REG_RPM_LSB, REG_COOLANT, REG_SPEED, REG_BATTERY)

CMD_INIT = b"\xFF\xFF\xEF"
INIT_ACK = 0x10
CMD_READ_REGISTER = 0x5A
CMD_START_STREAM = 0xF0
CMD_STOP = 0x30
FRAME_START = 0xFF


def build_stream_request(registers: Iterable[int] = STREAM_REGISTERS) -> bytes:
    out = bytearray()
    for reg in registers:
        out += bytes((CMD_READ_REGISTER, reg))
    out.append(CMD_START_STREAM)
    return bytes(out)


def decode_frame(frame: bytes, registers: Iterable[int] = STREAM_REGISTERS) -> EngineData:
    values = dict(zip(registers, frame))
    return EngineData(
        rpm=((values[REG_RPM_MSB] << 8) | values[REG_RPM_LSB]) * 12.5,
        coolant_c=values[REG_COOLANT] - 50,
        speed_kmh=values[REG_SPEED] * 2,
        battery_v=round(values[REG_BATTERY] * 0.08, 2),
    )


class FrameParser:
    """Splits the ECU byte stream into FF <n> <data...> frames.

    Data bytes can themselves be 0xFF, so a frame is only accepted once the
    byte after it is the start of the next frame. That costs one frame of
    latency (a few ms) but makes resynchronisation after noise reliable.
    """

    def __init__(self, length: int):
        self._length = length
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> List[bytes]:
        self._buf += data
        frames = []
        end = 2 + self._length
        while True:
            start = self._buf.find(FRAME_START)
            if start < 0:
                self._buf.clear()
                break
            del self._buf[:start]
            if len(self._buf) < 2:
                break
            if self._buf[1] != self._length:
                del self._buf[0]
                continue
            if len(self._buf) < end + 1:
                break
            if self._buf[end] != FRAME_START:
                del self._buf[0]
                continue
            frames.append(bytes(self._buf[2:end]))
            del self._buf[:end]
        return frames


def _open_serial(port: str, baudrate: int):
    import serial  # imported lazily so --simulate works without pyserial

    return serial.Serial(port, baudrate, timeout=0.1)


class ConsultClient:
    """Low-level request/response handling on the serial port."""

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        serial_factory: Callable[[str, int], object] = _open_serial,
        registers: Iterable[int] = STREAM_REGISTERS,
    ):
        self._port = port
        self._baudrate = baudrate
        self._factory = serial_factory
        self._registers = tuple(registers)
        self._parser = FrameParser(len(self._registers))
        self._ser = None

    def open(self) -> None:
        if self._ser is None:
            self._ser = self._factory(self._port, self._baudrate)
            log.info("Opened CONSULT adapter on %s", self._port)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def initialise(self, timeout: float = 1.0) -> bool:
        """Send the init sequence; True if the ECU acknowledged it."""
        self.stop_stream()
        self._ser.write(CMD_INIT)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = self._ser.read(1)
            if reply and reply[0] == INIT_ACK:
                return True
        return False

    def start_stream(self) -> None:
        self._parser.reset()
        self._ser.write(build_stream_request(self._registers))

    def stop_stream(self) -> None:
        self._ser.write(bytes((CMD_STOP,)))
        time.sleep(0.05)
        self._ser.reset_input_buffer()

    def read_frames(self) -> List[EngineData]:
        """Read whatever is available (blocks up to the port timeout)."""
        data = self._ser.read(max(1, self._ser.in_waiting))
        return [decode_frame(f, self._registers) for f in self._parser.feed(data)]


class ConsultPoller(threading.Thread):
    """Keeps the DataStore fresh and tells the PowerManager what it sees.

    Normal mode: continuous register stream (tens of frames per second).
    Sleep mode:  every `power.probe_interval` seconds, init the ECU, read a
                 single frame, stop again. When the ECU is unpowered
                 (ignition off) a probe is just 3 bytes out and a 1 s wait.
    """

    def __init__(
        self,
        client: ConsultClient,
        store: DataStore,
        power,
        stale_after: float = 2.0,
        retry_interval: float = 1.0,
    ):
        super().__init__(name="consult-poller", daemon=True)
        self._client = client
        self._store = store
        self._power = power
        self._stale_after = stale_after
        self._retry_interval = retry_interval
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._client.open()
            except OSError as exc:
                log.warning("CONSULT adapter not available: %s", exc)
                self._power.report(None)
                self._stop_evt.wait(5 * self._retry_interval)
                continue
            try:
                if self._power.sleeping:
                    self._probe()
                else:
                    self._stream()
            except OSError as exc:
                log.warning("CONSULT serial error: %s", exc)
                self._client.close()
                self._stop_evt.wait(self._retry_interval)
        try:
            self._client.stop_stream()
        except Exception:
            pass
        self._client.close()

    def _probe(self) -> None:
        data = None
        if self._client.initialise():
            self._client.start_stream()
            data = self._first_frame(timeout=1.0)
            self._client.stop_stream()
        if data is not None:
            self._store.update(data, valid_for=self._power.probe_interval + self._stale_after)
        self._power.report(data)
        if self._power.sleeping:
            self._stop_evt.wait(self._power.probe_interval)

    def _first_frame(self, timeout: float) -> Optional[EngineData]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop_evt.is_set():
            frames = self._client.read_frames()
            if frames:
                return frames[-1]
        return None

    def _stream(self) -> None:
        if not self._client.initialise():
            log.debug("ECU did not answer init")
            self._power.report(None)
            self._stop_evt.wait(self._retry_interval)
            return
        log.info("ECU connected, streaming registers")
        self._client.start_stream()
        last_frame = time.monotonic()
        while not self._stop_evt.is_set():
            frames = self._client.read_frames()
            now = time.monotonic()
            if frames:
                last_frame = now
                data = frames[-1]
                self._store.update(data, valid_for=self._stale_after)
                self._power.report(data)
                if self._power.sleeping:
                    break
            elif now - last_frame > self._stale_after:
                log.info("ECU stopped responding")
                self._power.report(None)
                return
        self._client.stop_stream()
