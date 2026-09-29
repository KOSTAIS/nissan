# consult2elm: Nissan CONSULT to ELM327 over Bluetooth

This program reads **engine RPM, coolant temperature and vehicle speed** from a
1990s Nissan (built for a 1997 Almera) through its **CONSULT** diagnostic port.
It then presents them as a Bluetooth **ELM327 OBD-II adapter**, so a
**Teltonika FMB130** (or any OBD app) can read them as standard OBD-II PIDs.

```
 Nissan ECU ──CONSULT──▶ CONSULT-USB cable ──USB──▶ Raspberry Pi Zero 2 W ──Bluetooth SPP──▶ FMB130
             (9600 baud)                            consult2elm              "ELM327 v1.5"
```

| Value           | CONSULT register | OBD-II PID | Encoding sent to tracker |
|-----------------|------------------|------------|--------------------------|
| Engine RPM      | 0x00 / 0x01      | 01 0C      | rpm × 4, 2 bytes         |
| Coolant temp    | 0x08             | 01 05      | °C + 40                  |
| Vehicle speed   | 0x0B             | 01 0D      | km/h                     |
| Battery voltage | 0x0C             | `ATRV`     | e.g. `14.1V`             |

The emulated adapter reports protocol **ISO 9141-2**, which is what a real
OBD-II car of that age would use. It answers the usual AT set-up commands
(`ATZ ATE0 ATL0 ATS0 ATH1 ATSP0 ATDP ATDPN ATRV ...`), `0100` (supported PIDs),
mode 03/07 (no fault codes) and returns `NO DATA` for anything else.

---

## 1. Install on the Pi

Use **Raspberry Pi OS Lite** (no desktop). It boots faster and draws less power.

```bash
sudo apt install -y git
git clone https://github.com/KOSTAIS/nissan && cd nissan
sudo ./scripts/install.sh 1234          # 1234 = Bluetooth PIN the FMB130 will use
sudo ./scripts/power-tweaks.sh          # optional, see section 4
sudo reboot
```

The install script:

* installs `python3-serial`, `bluez` and `bluez-tools`,
* copies the program to `/opt/consult2elm` and the config to `/etc/consult2elm.conf`,
* runs `bluetoothd --compat` so the Serial Port Profile record can be registered,
* enables two services: `consult2elm-agent` (PIN pairing) and `consult2elm` (the bridge).

At the end it prints the Pi's **Bluetooth MAC address**. You need it for the tracker.

Check it is running:

```bash
journalctl -u consult2elm -f
```

## 2. Configure the FMB130

In the **Teltonika Configurator** (menu names can differ slightly between firmware versions):

1. **Bluetooth**: BT radio *Enabled*. Set the connection mode to **OBDII dongle**
   (or *Hands-free + OBDII*).
2. **External device**: enter the Pi's **MAC address** (preferred) or the name
   `OBDII`, and the **PIN** (`1234` unless you changed it).
3. **OBD II** tab: enable **Engine RPM**, **Vehicle Speed** and **Coolant
   Temperature**. Set their priority and send period the same way as any other I/O.

When the tracker has paired once, set `bt_discoverable = no` in
`/etc/consult2elm.conf` and run `sudo systemctl restart consult2elm`. The tracker
reconnects by MAC address, and the Pi no longer answers Bluetooth scans.

The FMB130 asks for other PIDs too (fuel level, MAF, …). Those get `NO DATA`,
which is normal for an adapter on a car that does not support them.

## 3. Test without the car

The ECU simulator lets you try everything on a desk:

```bash
# On the Pi, over Bluetooth, with a phone app such as "Car Scanner" or "Torque":
sudo systemctl stop consult2elm
sudo python3 -m consult2elm --simulate

# Or on any computer, over TCP (like a Wi-Fi ELM327):
python3 -m consult2elm --simulate --tcp 35000 -c /dev/null
nc -C localhost 35000     # type ATZ, 010C, 0105, 010D (-C sends CR+LF on Enter)
```

To test with the real adapter but without Bluetooth, use `--tcp 35000 --port /dev/ttyUSB0 -v`.

Unit and integration tests (need `pytest` and `pyserial`):

```bash
python3 -m pytest -q
```

## 4. Power saving and waking on RPM

A Pi Zero 2 W cannot suspend to RAM. There are two sleep levels: a **software
sleep** that is always available, and a **halt** that needs a small wake-up
circuit or a switched power supply.

### 4a. Software low-power mode (built in, always on)

* While the engine runs, the ECU streams frames continuously (many per second).
* After `idle_after` seconds (default 120) **without RPM**, the Pi enters low-power mode:
  * the stream stops, and the ECU is only **probed every `probe_interval` seconds**
    (init + one frame + stop). With the ignition off the ECU is unpowered, so a
    probe is 3 bytes sent and a 1 s wait.
  * CPU cores 1-3 go offline and the cpufreq governor switches to `powersave`.
* As soon as a probe sees **RPM > 0**, it wakes up and streams again. This is the
  "wake on RPM" you asked for. It works entirely through the CONSULT cable.

Set `wake_on = ecu` if "ignition on" should already count as activity.

`scripts/power-tweaks.sh` also turns off the activity LED, audio and unneeded
background services. `--disable-wifi` turns off Wi-Fi as well, but only do this
once everything works, because you will lose SSH access.

This mode still draws roughly what an idle Pi Zero 2 W draws, on the order of
100 mA at 5 V. Measure your own setup with a USB power meter. It is fine for
daily driving, but it will drain a car battery if the car stands for weeks.

### 4b. Real "ultra sleep": halt, and wake on engine start

The Pi cannot see RPM while it is halted, and the ECU is unpowered with the
ignition off, so the CONSULT port cannot wake anything. The wake signal must
come from hardware. The most practical signal is **alternator voltage**. A
charging system reads about 13.6–14.4 V with the engine running and about
12.4–12.7 V with it off, so "voltage > 13.2 V" in practice means "engine is
turning". The FMB130 uses the same trick for its own ignition detection.

Pick one:

| Option | What you build | Draw with engine off | Notes |
|---|---|---|---|
| **A. Voltage-sensing relay** (recommended) | 12 V → *voltage-sensing relay / smart battery isolator* (switches on ≈13.3 V, off ≈12.8 V with delay) → 12→5 V buck → Pi | ≈ 0 (relay's own few mA) | Simplest true wake on engine running. The power cut is abrupt, so enable the read-only overlay FS (below). |
| **B. Ignition-switched supply** | Fuse-tap on an ignition-switched fuse → 12→5 V buck → Pi | 0 | Wakes on key-on instead of engine running. Same SD-card advice. |
| **C. Halt + GPIO3 wake** | Pi on permanent 12→5 V. A comparator/transistor pulls **GPIO3 (pin 5) to GND** while voltage > 13.2 V. Set `shutdown_after = 600`. | Pi halted: much lower than idle (measure it) + buck quiescent | Clean shutdown by software, then the Pi boots again when the engine starts, because pulling GPIO3 low wakes a halted Pi. |

Option C, conceptually:

```
 +12V (battery) ──[ 13.2 V threshold detector, e.g. LM393 comparator or  ]
                  [ zener + NPN transistor, open-collector output        ]──── GPIO3 (pin 5)
 GND ───────────────────────────────────────────────────────────────────────── GND   (pin 6)
```

The output must only **sink to GND** (open collector/drain). Never put car
voltage on a GPIO. Do not enable the `gpio-shutdown` overlay on GPIO3 with this
circuit, because it would shut the Pi down when the engine starts.

Whichever option you pick:

* Use a **good 12→5 V buck converter** with low quiescent current. Cheap USB car
  chargers often waste more power than the Pi when idle.
* For options A/B, enable the read-only root filesystem so sudden power loss
  cannot corrupt the SD card: `sudo raspi-config nonint enable_overlayfs`. After
  that, configuration changes need the overlay disabled first.
* Boot takes about 20–30 s on Pi OS Lite. The tracker starts getting values after that.

## 5. Configuration reference

All settings live in `/etc/consult2elm.conf` (see `consult2elm.conf.example`):

| Section | Key | Default | Meaning |
|---|---|---|---|
| consult | `serial_port` | `/dev/ttyUSB0` | CONSULT cable. `/dev/serial/by-id/...` is more stable. |
| consult | `stale_after` | `2` | Seconds before values become `NO DATA` |
| obd | `transport` | `bluetooth` | `bluetooth` or `tcp` |
| obd | `bt_name` | `OBDII` | Visible Bluetooth name |
| obd | `bt_channel` | `1` | RFCOMM channel of the SPP service |
| obd | `bt_discoverable` | `yes` | Set `no` after the tracker has paired |
| power | `wake_on` | `rpm` | `rpm` or `ecu` (ignition) |
| power | `idle_after` | `120` | Seconds without activity before low-power mode |
| power | `probe_interval` | `5` | ECU probe period while sleeping |
| power | `cpu_saving` | `yes` | Offline CPU cores 1-3 + powersave governor while sleeping |
| power | `shutdown_after` | `0` | Halt after this many idle seconds (0 = never). Needs option C. |

## 6. Troubleshooting

### Debugging

* **Debug output:** add `--debug`. It shows every Bluetooth setup command with
  its output, every OBD request/reply (`<-` / `->`), and a status line every 5 s:
  `sudo python3 -m consult2elm --simulate --debug`
* **Bluetooth health check:** `sudo ./scripts/bt-diag.sh` prints `[OK]`/`[FAIL]`
  for every requirement (rfkill, powered, discoverable, SPP record, pairing
  agent, `--compat`) plus the related logs. Paste its output when asking for help.
* At start-up the program logs one line with the adapter state, e.g.
  `Bluetooth adapter B8:27:EB:..: alias=OBDII powered=yes discoverable=yes ...`,
  and an ERROR line if the phone/tracker cannot see it.

### Phone cannot find "OBDII"

* **iPhones cannot be used for this test.** iOS does not show or pair classic
  Bluetooth serial (SPP) devices, so Bluetooth ELM327 adapters never appear on
  an iPhone. Use an Android phone (e.g. the "Car Scanner" app), or test over
  TCP with `--tcp 35000`.
* On Android, search from the phone's **Bluetooth settings** screen while the
  program is running. Many OBD apps only list already paired devices.
* Run `sudo ./scripts/bt-diag.sh` and fix every `[FAIL]` line.

* **`ECU did not answer init`** (run with `-v`): the ignition is off, the cable
  is on the wrong port, or the port name is wrong. `ls /dev/serial/by-id/` shows
  the adapter.
* **Tracker does not connect**: check that `sdptool browse local` lists
  "Serial Port", and look at `journalctl -u consult2elm-agent` while the tracker
  pairs. Re-pair after changing the PIN.
* **Values look wrong**: the register scaling follows the common Consult-I
  documentation for ECCS ECUs (RPM ×12.5, coolant −50 °C, speed ×2 km/h). If
  your ECU differs, adjust `decode_frame()` in `consult2elm/consult.py`.

## Code layout

```
consult2elm/
  consult.py   CONSULT protocol: init, register stream, frame parser, poller thread
  elm327.py    ELM327 command interpreter (AT commands, mode 01/03/04/07)
  server.py    RFCOMM (Bluetooth SPP) or TCP server
  power.py     low-power mode, CPU hot-unplug, optional halt
  btsetup.py   Bluetooth name / discoverable / SPP record
  fake_ecu.py  simulated ECU for --simulate and the tests
  main.py      configuration and wiring
```
