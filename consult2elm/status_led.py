"""Show the bridge state on the Pi's green activity LED (no SSH needed).

Every 2 seconds the LED blinks N times:

  1 blink   running, but no ECU data and no OBD client (tracker) connected
  2 blinks  ECU data OK, tracker NOT connected
  3 blinks  tracker connected, but NO ECU data (ignition off / cable problem)
  steady on everything OK: ECU data and tracker connected
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

log = logging.getLogger(__name__)

LED_CANDIDATES = ("/sys/class/leds/ACT", "/sys/class/leds/led0")
CYCLE = 2.0
BLINK = 0.15


def blink_count(ecu_ok: bool, client_connected: bool) -> Optional[int]:
    """Blinks per cycle, or None for steady on."""
    if ecu_ok and client_connected:
        return None
    if ecu_ok:
        return 2
    if client_connected:
        return 3
    return 1


class StatusLed(threading.Thread):
    def __init__(self, store, server, stop: threading.Event, led_path: Optional[str] = None):
        super().__init__(name="status-led", daemon=True)
        self._store = store
        self._server = server
        self._stop_evt = stop
        self._path = led_path or next((p for p in LED_CANDIDATES if os.path.isdir(p)), None)
        self._saved_trigger: Optional[str] = None

    def run(self) -> None:
        if not self._path or not self._take_over():
            log.info("Status LED not available (no writable /sys/class/leds/ACT)")
            return
        log.info("Status LED on %s: 1 blink=idle, 2=ECU ok/no tracker, 3=tracker/no ECU, steady=all OK",
                 self._path)
        try:
            while not self._stop_evt.is_set():
                count = blink_count(self._store.current() is not None, self._server.client_count > 0)
                if count is None:
                    self._set(True)
                    self._stop_evt.wait(CYCLE)
                    continue
                for _ in range(count):
                    self._set(True)
                    self._stop_evt.wait(BLINK)
                    self._set(False)
                    self._stop_evt.wait(BLINK * 2)
                self._stop_evt.wait(CYCLE - count * BLINK * 3)
        finally:
            self._restore()

    def _take_over(self) -> bool:
        try:
            with open(f"{self._path}/trigger") as f:
                current = f.read()
            if "[" in current:
                self._saved_trigger = current.split("[", 1)[1].split("]", 1)[0]
            with open(f"{self._path}/trigger", "w") as f:
                f.write("none")
            return True
        except OSError:
            return False

    def _set(self, on: bool) -> None:
        try:
            with open(f"{self._path}/brightness", "w") as f:
                f.write("1" if on else "0")
        except OSError:
            pass

    def _restore(self) -> None:
        if self._saved_trigger:
            try:
                with open(f"{self._path}/trigger", "w") as f:
                    f.write(self._saved_trigger)
            except OSError:
                pass
