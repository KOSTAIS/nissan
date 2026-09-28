#!/bin/bash
# Reduce the Raspberry Pi Zero 2 W idle power draw. Run with sudo, then reboot.
#   --disable-wifi   also switch off Wi-Fi (you lose SSH over Wi-Fi!)
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo $0" >&2
    exit 1
fi

DISABLE_WIFI=0
[ "${1:-}" = "--disable-wifi" ] && DISABLE_WIFI=1

CONFIG=/boot/firmware/config.txt
[ -f "$CONFIG" ] || CONFIG=/boot/config.txt

BEGIN="# >>> consult2elm power tweaks"
END="# <<< consult2elm power tweaks"
sed -i "/^$BEGIN/,/^$END/d" "$CONFIG"
{
    echo "$BEGIN"
    echo "# Activity LED off"
    echo "dtparam=act_led_trigger=none"
    echo "dtparam=act_led_activelow=on"
    echo "# No audio, no camera/display probing"
    echo "dtparam=audio=off"
    echo "camera_auto_detect=0"
    echo "display_auto_detect=0"
    if [ "$DISABLE_WIFI" = 1 ]; then
        echo "dtoverlay=disable-wifi"
    fi
    echo "$END"
} >> "$CONFIG"

# Background services that only cost CPU wake-ups in a car.
for unit in triggerhappy.service avahi-daemon.service avahi-daemon.socket \
            ModemManager.service cups.service cups-browsed.service \
            apt-daily.timer apt-daily-upgrade.timer man-db.timer; do
    if systemctl list-unit-files "$unit" >/dev/null 2>&1; then
        systemctl disable --now "$unit" 2>/dev/null || true
    fi
done

echo "Done. Reboot to apply $CONFIG changes."
echo "Tip: 'sudo raspi-config nonint enable_overlayfs' makes the SD card"
echo "read-only so cutting the power without shutdown cannot corrupt it."
