"""Prepare the BlueZ adapter so the FMB130 can find and pair with the Pi."""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Dict, Tuple

log = logging.getLogger(__name__)

# bluetoothctl often exits 0 even when the command failed, so its output is
# checked for these as well.
_FAILURE_MARKERS = ("Failed", "failed", "not available", "No default controller", "Invalid", "org.bluez.Error")


def _run(*cmd: str, quiet: bool = False) -> Tuple[bool, str]:
    log.debug("$ %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if not quiet:
            log.warning("%s failed: %s", " ".join(cmd), exc)
        return False, str(exc)
    output = (result.stdout + result.stderr).strip()
    for line in output.splitlines():
        log.debug("  %s", line)
    ok = result.returncode == 0 and not any(m in output for m in _FAILURE_MARKERS)
    if not ok and not quiet:
        log.warning("%s failed: %s", " ".join(cmd), output or f"exit code {result.returncode}")
    return ok, output


def adapter_state() -> Dict[str, str]:
    """Parse `bluetoothctl show` into {"Powered": "yes", "Alias": ..., ...}."""
    _, output = _run("bluetoothctl", "show", quiet=True)
    state: Dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Controller "):
            state["Address"] = line.split()[1]
        elif ":" in line:
            key, _, value = line.partition(":")
            if key in ("Name", "Alias", "Class", "Powered", "Discoverable", "Pairable", "DiscoverableTimeout"):
                state[key] = value.strip()
    return state


def _power_on() -> bool:
    for attempt in range(1, 4):
        ok, _ = _run("bluetoothctl", "power", "on", quiet=attempt < 3)
        if ok or adapter_state().get("Powered") == "yes":
            return True
        log.debug("Bluetooth power on failed (attempt %d), retrying", attempt)
        _run("rfkill", "unblock", "bluetooth", quiet=True)
        time.sleep(1)
    return False


def configure_adapter(name: str, channel: int, discoverable: bool) -> Dict[str, str]:
    """Power on, set the visible name, advertise a Serial Port Profile record.

    Pairing itself (PIN) is handled by the bt-agent service, see
    systemd/consult2elm-agent.service. `sdptool add` needs bluetoothd to run
    with --compat, which scripts/install.sh sets up.

    Returns the adapter state afterwards and logs any problem found.
    """
    # Raspberry Pi OS soft-blocks the radios until a Wi-Fi country is set;
    # a blocked adapter makes "power on" fail with org.bluez.Error.Failed.
    _run("rfkill", "unblock", "bluetooth")
    if not _power_on():
        log.error("Could not power on Bluetooth. Check: rfkill list; sudo systemctl status bluetooth")

    _run("bluetoothctl", "system-alias", name)
    _run("bluetoothctl", "pairable", "on")
    if discoverable:
        _run("bluetoothctl", "discoverable-timeout", "0")
        _run("bluetoothctl", "discoverable", "on")
    else:
        # Already paired: stay connectable for the tracker but invisible to
        # scans, which also saves a little radio power.
        _run("bluetoothctl", "discoverable", "off")

    state = adapter_state()
    if discoverable and state.get("Discoverable") != "yes":
        # Fallback via the older HCI tool: page + inquiry scan = visible.
        log.info("Adapter not discoverable yet, trying hciconfig piscan")
        _run("hciconfig", "hci0", "piscan")
        state = adapter_state()

    if not _run("sdptool", "add", f"--channel={channel}", "SP")[0]:
        log.warning("Could not register the SPP service record; is bluetoothd running with --compat? "
                    "(sudo ./scripts/install.sh sets that up)")

    log.info(
        "Bluetooth adapter %s: alias=%s powered=%s discoverable=%s pairable=%s class=%s",
        state.get("Address", "?"), state.get("Alias", "?"), state.get("Powered", "?"),
        state.get("Discoverable", "?"), state.get("Pairable", "?"), state.get("Class", "?"),
    )
    if not state:
        log.error("No Bluetooth adapter found (bluetoothctl show returned nothing)")
    elif state.get("Powered") != "yes":
        log.error("Bluetooth is not powered; phones and the tracker cannot see the Pi")
    elif discoverable and state.get("Discoverable") != "yes":
        log.error("Bluetooth is not discoverable; phones will not find '%s'", name)
    return state
