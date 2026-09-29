#!/bin/bash
# Install consult2elm on Raspberry Pi OS (Bookworm or later). Run with sudo.
set -euo pipefail
trap 'echo "install.sh failed at line $LINENO: $BASH_COMMAND" >&2' ERR

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo $0" >&2
    exit 1
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
PIN="${1:-1234}"

apt-get update
apt-get install -y python3-serial bluez bluez-tools

install -d /opt/consult2elm
rm -rf /opt/consult2elm/consult2elm
cp -r "$SRC/consult2elm" /opt/consult2elm/

if [ ! -f /etc/consult2elm.conf ]; then
    install -m 644 "$SRC/consult2elm.conf.example" /etc/consult2elm.conf
fi

install -d -m 700 /etc/consult2elm
if [ ! -f /etc/consult2elm/bt-pins ]; then
    echo "* $PIN" > /etc/consult2elm/bt-pins
    chmod 600 /etc/consult2elm/bt-pins
fi

# sdptool (Serial Port Profile record) needs bluetoothd in compatibility mode.
BTD=""
for candidate in /usr/libexec/bluetooth/bluetoothd /usr/lib/bluetooth/bluetoothd; do
    if [ -x "$candidate" ]; then
        BTD="$candidate"
        break
    fi
done
if [ -z "$BTD" ]; then
    echo "bluetoothd not found" >&2
    exit 1
fi
install -d /etc/systemd/system/bluetooth.service.d
cat > /etc/systemd/system/bluetooth.service.d/consult2elm.conf <<CONF
[Service]
ExecStart=
ExecStart=$BTD --compat
CONF

install -m 644 "$SRC/systemd/consult2elm.service" /etc/systemd/system/
install -m 644 "$SRC/systemd/consult2elm-agent.service" /etc/systemd/system/
systemctl daemon-reload
systemctl restart bluetooth
systemctl enable --now consult2elm-agent.service consult2elm.service

echo
echo "Installed. Bluetooth name: see bt_name in /etc/consult2elm.conf, PIN: $PIN"
echo "Adapter MAC (enter this in the FMB130 configurator):"
bluetoothctl show | awk '/^Controller/ && !mac {mac = $2} END {print "  " mac}' || true
echo "Service status:"
systemctl --no-pager --lines=0 status consult2elm.service || true
echo "Logs: journalctl -u consult2elm -f"
