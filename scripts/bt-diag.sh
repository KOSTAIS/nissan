#!/bin/bash
# Collect everything needed to debug "the phone / tracker cannot see or pair
# with the Pi". Run with sudo and paste the output when asking for help.
#   sudo ./scripts/bt-diag.sh

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo $0" >&2
    exit 1
fi

section() { printf '\n===== %s =====\n' "$1"; }
ok()   { printf '  [OK]   %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; }

section "Quick checks"
if rfkill list bluetooth 2>/dev/null | grep -q "Soft blocked: yes"; then
    bad "Bluetooth is soft-blocked (fix: sudo rfkill unblock bluetooth)"
else
    ok "Bluetooth not blocked by rfkill"
fi
systemctl is-active --quiet bluetooth && ok "bluetooth service running" || bad "bluetooth service not running"
if pgrep -a bluetoothd | grep -qE -- "(--compat|-C)"; then
    ok "bluetoothd runs with --compat (needed for the SPP record)"
else
    bad "bluetoothd runs WITHOUT --compat (fix: sudo ./scripts/install.sh, then reboot)"
fi
SHOW="$(bluetoothctl show 2>&1)"
echo "$SHOW" | grep -q "Powered: yes"      && ok "adapter powered"      || bad "adapter NOT powered (fix: sudo bluetoothctl power on)"
echo "$SHOW" | grep -q "Discoverable: yes" && ok "adapter discoverable" || bad "adapter NOT discoverable (fix: sudo bluetoothctl discoverable on)"
echo "$SHOW" | grep -q "Pairable: yes"     && ok "adapter pairable"     || bad "adapter NOT pairable (fix: sudo bluetoothctl pairable on)"
if sdptool browse local 2>/dev/null | grep -q "Serial Port"; then
    ok "Serial Port (SPP) service record registered"
else
    bad "no Serial Port service record (is consult2elm running? needs --compat)"
fi
systemctl is-active --quiet consult2elm-agent && ok "pairing agent (consult2elm-agent) running" \
    || bad "pairing agent not running: pairing with PIN will fail (fix: sudo systemctl start consult2elm-agent)"
if systemctl is-active --quiet consult2elm; then
    ok "consult2elm service running"
elif pgrep -f "python3 -m consult2elm" >/dev/null; then
    ok "consult2elm running by hand"
else
    bad "consult2elm not running (sudo systemctl start consult2elm, or run it by hand)"
fi

section "rfkill list"
rfkill list

section "bluetoothctl show"
echo "$SHOW"

section "hciconfig -a"
hciconfig -a 2>&1 || true

section "bluetoothd process"
pgrep -a bluetoothd || echo "not running"

section "SDP records (sdptool browse local)"
sdptool browse local 2>&1 | grep -E "Service Name|Channel" || echo "none / sdptool failed"

section "Paired devices"
bluetoothctl devices Paired 2>/dev/null || bluetoothctl paired-devices 2>/dev/null

section "PIN file"
if [ -f /etc/consult2elm/bt-pins ]; then echo "present ($(wc -l < /etc/consult2elm/bt-pins) line(s))"; else echo "missing"; fi

section "Last log lines: bluetooth"
journalctl -u bluetooth -n 20 --no-pager 2>&1
section "Last log lines: consult2elm-agent"
journalctl -u consult2elm-agent -n 20 --no-pager 2>&1
section "Last log lines: consult2elm"
journalctl -u consult2elm -n 30 --no-pager 2>&1

section "System"
uname -a
grep PRETTY_NAME /etc/os-release
bluetoothctl --version 2>&1
