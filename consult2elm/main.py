"""Entry point: wire CONSULT poller, power manager and ELM327 server together."""

from __future__ import annotations

import argparse
import configparser
import logging
import shlex
import signal
import threading
from dataclasses import dataclass, fields

from .btsetup import configure_adapter
from .consult import ConsultClient, ConsultPoller
from .data import DataStore
from .power import PowerManager
from .server import ElmServer

log = logging.getLogger("consult2elm")

DEFAULT_CONFIG = "/etc/consult2elm.conf"


@dataclass
class Config:
    # [consult]
    serial_port: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    stale_after: float = 2.0
    # [obd]
    transport: str = "bluetooth"
    bt_name: str = "OBDII"
    bt_channel: int = 1
    bt_discoverable: bool = True
    bt_setup: bool = True
    tcp_port: int = 35000
    # [power]
    wake_on: str = "rpm"
    idle_after: float = 120.0
    probe_interval: float = 5.0
    cpu_saving: bool = True
    shutdown_after: float = 0.0
    shutdown_command: str = "systemctl poweroff"
    # [general]
    simulate: bool = False
    log_level: str = "INFO"

    SECTIONS = {
        "consult": ("serial_port", "baudrate", "stale_after"),
        "obd": ("transport", "bt_name", "bt_channel", "bt_discoverable", "bt_setup", "tcp_port"),
        "power": ("wake_on", "idle_after", "probe_interval", "cpu_saving", "shutdown_after", "shutdown_command"),
        "general": ("simulate", "log_level"),
    }

    @classmethod
    def load(cls, path: str) -> "Config":
        cfg = cls()
        parser = configparser.ConfigParser()
        if not parser.read(path):
            return cfg
        types = {f.name: f.type for f in fields(cls)}
        for section, keys in cls.SECTIONS.items():
            if not parser.has_section(section):
                continue
            for key in keys:
                if not parser.has_option(section, key):
                    continue
                kind = types[key]
                if kind in (bool, "bool"):
                    value = parser.getboolean(section, key)
                elif kind in (int, "int"):
                    value = parser.getint(section, key)
                elif kind in (float, "float"):
                    value = parser.getfloat(section, key)
                else:
                    value = parser.get(section, key)
                setattr(cfg, key, value)
        return cfg


def parse_args(argv=None) -> Config:
    ap = argparse.ArgumentParser(description="Nissan CONSULT to ELM327 (OBD-II) bridge")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="config file (default: %(default)s)")
    ap.add_argument("--port", help="CONSULT serial port, e.g. /dev/ttyUSB0")
    ap.add_argument("--tcp", type=int, metavar="PORT", help="serve ELM327 over TCP instead of Bluetooth")
    ap.add_argument("--simulate", action="store_true", help="use a simulated ECU instead of the adapter")
    ap.add_argument("-v", "--verbose", "--debug", dest="verbose", action="store_true",
                    help="debug logging: Bluetooth setup commands, every OBD request/reply, status every 5 s")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    if args.port:
        cfg.serial_port = args.port
    if args.tcp:
        cfg.transport, cfg.tcp_port = "tcp", args.tcp
    if args.simulate:
        cfg.simulate = True
    if args.verbose:
        cfg.log_level = "DEBUG"
    return cfg


def _log_status(store: DataStore, power: PowerManager, server: ElmServer, stop: threading.Event) -> None:
    """Periodic one-line summary: every 5 s with --debug, otherwise every 60 s."""
    interval = 5 if log.isEnabledFor(logging.DEBUG) else 60
    while not stop.wait(interval):
        data = store.current()
        engine = (
            f"rpm={data.rpm:.0f} coolant={data.coolant_c}C speed={data.speed_kmh}km/h battery={data.battery_v:.1f}V"
            if data else "no ECU data"
        )
        log.info("Status: %s | %s | OBD clients connected: %d",
                 engine, "low-power" if power.sleeping else "active", server.client_count)


def main(argv=None) -> int:
    cfg = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    store = DataStore()
    power = PowerManager(
        wake_on=cfg.wake_on,
        idle_after=cfg.idle_after,
        probe_interval=cfg.probe_interval,
        shutdown_after=cfg.shutdown_after,
        shutdown_command=shlex.split(cfg.shutdown_command),
        cpu_saving=cfg.cpu_saving and not cfg.simulate,
    )

    if cfg.simulate:
        from .fake_ecu import FakeConsultECU

        ecu = FakeConsultECU()
        ecu.animate = True
        client = ConsultClient("simulated", serial_factory=lambda port, baud: ecu)
        log.info("Using simulated ECU")
    else:
        client = ConsultClient(cfg.serial_port, cfg.baudrate)

    if cfg.transport == "bluetooth" and cfg.bt_setup:
        configure_adapter(cfg.bt_name, cfg.bt_channel, cfg.bt_discoverable)

    server = ElmServer(store, transport=cfg.transport, channel=cfg.bt_channel, tcp_port=cfg.tcp_port)
    poller = ConsultPoller(client, store, power, stale_after=cfg.stale_after)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    poller.start()
    threading.Thread(target=_log_status, args=(store, power, server, stop), name="status", daemon=True).start()
    try:
        server.serve_forever(stop)
    finally:
        poller.stop()
        poller.join(timeout=5)
        power.restore()
    return 0
