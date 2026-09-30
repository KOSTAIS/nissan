"""Low-power handling for the Raspberry Pi.

The Pi Zero 2 W has no suspend-to-RAM, so "sleep" here means:

  * stop the continuous CONSULT stream and only probe the ECU every few
    seconds (wake-up happens when RPM > 0, or when the ECU answers at all),
  * take CPU cores 1-3 offline and switch cpufreq to "powersave",
  * optionally, after a longer idle period, halt the Pi completely. From halt
    it draws far less and is booted again by pulling GPIO3 to GND (or by
    power being re-applied), e.g. from an engine-running detector, see README.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Callable, Optional, Sequence

from .data import EngineData

log = logging.getLogger(__name__)

CPU_SYSFS = "/sys/devices/system/cpu"


class PowerManager:
    def __init__(
        self,
        wake_on: str = "rpm",
        idle_after: float = 120.0,
        probe_interval: float = 5.0,
        shutdown_after: float = 0.0,
        shutdown_command: Sequence[str] = ("systemctl", "poweroff"),
        cpu_saving: bool = True,
        cpu_sysfs: str = CPU_SYSFS,
        clock: Callable[[], float] = time.monotonic,
        runner: Callable = subprocess.run,
    ):
        if wake_on not in ("rpm", "ecu"):
            raise ValueError("wake_on must be 'rpm' or 'ecu'")
        self.wake_on = wake_on
        self.idle_after = idle_after
        self.probe_interval = probe_interval
        self.shutdown_after = shutdown_after
        self.shutdown_command = list(shutdown_command)
        self.cpu_saving = cpu_saving
        self._sysfs = cpu_sysfs
        self._clock = clock
        self._runner = runner
        self._lock = threading.Lock()
        self._last_active = clock()
        self._saved_governor: Optional[str] = None
        self.sleeping = False
        self.shutdown_requested = False
        self._hotplug_ok = True  # some kernels do not allow taking CPUs offline

    def is_active(self, data: Optional[EngineData]) -> bool:
        if data is None:
            return False
        return self.wake_on == "ecu" or data.rpm > 0

    def report(self, data: Optional[EngineData]) -> None:
        """Called by the poller with every frame, or None if the ECU is silent."""
        with self._lock:
            now = self._clock()
            if self.is_active(data):
                self._last_active = now
                if self.sleeping:
                    self._wake()
                return
            idle = now - self._last_active
            if not self.sleeping and idle >= self.idle_after:
                self._sleep(idle)
            if self.shutdown_after > 0 and idle >= self.shutdown_after and not self.shutdown_requested:
                self._shutdown(idle)

    def restore(self) -> None:
        """Undo CPU changes (called on program exit)."""
        with self._lock:
            if self.sleeping:
                self._wake()

    # -- transitions -----------------------------------------------------------

    def _sleep(self, idle: float) -> None:
        log.info("No engine activity for %.0f s, entering low-power mode", idle)
        self.sleeping = True
        if self.cpu_saving:
            self._saved_governor = self._read(f"{self._sysfs}/cpu0/cpufreq/scaling_governor")
            self._write(f"{self._sysfs}/cpu0/cpufreq/scaling_governor", "powersave")
            for cpu in self._secondary_cpus() if self._hotplug_ok else []:
                if not self._write(f"{self._sysfs}/{cpu}/online", "0"):
                    self._hotplug_ok = False
                    log.info("This kernel does not allow taking CPU cores offline; only the "
                             "powersave governor is used in low-power mode")
                    break

    def _wake(self) -> None:
        log.info("Engine activity detected, leaving low-power mode")
        self.sleeping = False
        if self.cpu_saving:
            for cpu in self._secondary_cpus() if self._hotplug_ok else []:
                self._write(f"{self._sysfs}/{cpu}/online", "1")
            if self._saved_governor:
                self._write(f"{self._sysfs}/cpu0/cpufreq/scaling_governor", self._saved_governor)

    def _shutdown(self, idle: float) -> None:
        self.shutdown_requested = True
        log.warning("No engine activity for %.0f s, halting: %s", idle, " ".join(self.shutdown_command))
        try:
            self._runner(self.shutdown_command, check=False)
        except OSError as exc:
            log.error("Shutdown command failed: %s", exc)

    # -- sysfs helpers -----------------------------------------------------------

    def _secondary_cpus(self):
        try:
            names = os.listdir(self._sysfs)
        except OSError:
            return []
        cpus = [n for n in names if n.startswith("cpu") and n[3:].isdigit() and n != "cpu0"]
        return sorted(cpus, key=lambda n: int(n[3:]))

    @staticmethod
    def _read(path: str) -> Optional[str]:
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError:
            return None

    @staticmethod
    def _write(path: str, value: str) -> bool:
        try:
            with open(path, "w") as f:
                f.write(value)
            return True
        except OSError as exc:
            log.debug("Cannot write %s: %s", path, exc)
            return False
