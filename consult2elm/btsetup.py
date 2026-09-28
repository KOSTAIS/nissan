"""Prepare the BlueZ adapter so the FMB130 can find and pair with the Pi."""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)


def _run(*cmd: str) -> bool:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("%s failed: %s", " ".join(cmd), exc)
        return False
    if result.returncode != 0:
        log.warning("%s failed: %s", " ".join(cmd), (result.stderr or result.stdout).strip())
        return False
    return True


def configure_adapter(name: str, channel: int, discoverable: bool) -> None:
    """Power on, set the visible name, advertise a Serial Port Profile record.

    Pairing itself (PIN) is handled by the bt-agent service, see
    systemd/consult2elm-agent.service. `sdptool add` needs bluetoothd to run
    with --compat, which scripts/install.sh sets up.
    """
    _run("bluetoothctl", "power", "on")
    _run("bluetoothctl", "system-alias", name)
    _run("bluetoothctl", "pairable", "on")
    if discoverable:
        _run("bluetoothctl", "discoverable-timeout", "0")
        _run("bluetoothctl", "discoverable", "on")
    else:
        # Already paired: stay connectable for the tracker but invisible to
        # scans, which also saves a little radio power.
        _run("bluetoothctl", "discoverable", "off")
    if not _run("sdptool", "add", f"--channel={channel}", "SP"):
        log.warning("Could not register the SPP service record; the tracker may not find the serial port")
