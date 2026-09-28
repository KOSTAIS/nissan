"""Thread-safe store for the latest engine values read from the ECU."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class EngineData:
    rpm: float          # revolutions per minute
    coolant_c: int      # coolant temperature, degrees Celsius
    speed_kmh: int      # vehicle speed, km/h
    battery_v: float    # ECU supply voltage, volts


class DataStore:
    """Holds the most recent EngineData and how long it stays valid.

    The Consult poller writes, the ELM327 emulator reads. Values expire so the
    tracker gets "NO DATA" instead of a frozen RPM when the ECU goes away.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._data: Optional[EngineData] = None
        self._valid_until = 0.0

    def update(self, data: EngineData, valid_for: float) -> None:
        with self._lock:
            self._data = data
            self._valid_until = self._clock() + valid_for

    def current(self) -> Optional[EngineData]:
        """Latest values, or None if they are stale or were never read."""
        with self._lock:
            if self._data is None or self._clock() > self._valid_until:
                return None
            return self._data

    def last_known(self) -> Optional[EngineData]:
        """Latest values regardless of age."""
        with self._lock:
            return self._data
