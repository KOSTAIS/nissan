import socket
import threading
import time

from consult2elm.consult import ConsultClient, ConsultPoller
from consult2elm.data import DataStore
from consult2elm.fake_ecu import FakeConsultECU
from consult2elm.power import PowerManager
from consult2elm.server import ElmServer


def query(sock, cmd):
    sock.sendall(cmd.encode() + b"\r")
    buf = b""
    while not buf.endswith(b">"):
        buf += sock.recv(256)
    return buf.decode()


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_end_to_end_over_tcp_with_sleep_and_wake(tmp_path):
    ecu = FakeConsultECU()
    ecu.rpm, ecu.coolant_c, ecu.speed_kmh = 3000, 92, 100
    store = DataStore()
    power = PowerManager(idle_after=0.5, probe_interval=0.3, cpu_saving=False)
    client = ConsultClient("fake", serial_factory=lambda p, b: ecu)
    poller = ConsultPoller(client, store, power, stale_after=1.0, retry_interval=0.1)
    server = ElmServer(store, transport="tcp", tcp_host="127.0.0.1", tcp_port=0)
    server.listen()
    stop = threading.Event()
    threading.Thread(target=server.serve_forever, args=(stop,), daemon=True).start()
    poller.start()
    try:
        with socket.create_connection(server.address, timeout=5) as sock:
            query(sock, "ATZ")
            query(sock, "ATE0")
            assert wait_for(lambda: store.current() is not None)
            assert query(sock, "010C") == "41 0C 2E E0\r\r>"   # 3000 rpm
            assert query(sock, "010D") == "41 0D 64\r\r>"      # 100 km/h
            assert query(sock, "0105") == "41 05 84\r\r>"      # 92 degC

            ecu.engine_running = False                        # engine stops
            assert wait_for(lambda: power.sleeping)
            assert query(sock, "010C") == "41 0C 00 00\r\r>"   # probes keep RPM=0 fresh

            ecu.ignition = False                              # key off
            assert wait_for(lambda: store.current() is None)
            assert query(sock, "010C") == "NO DATA\r\r>"

            ecu.ignition = True                               # engine started again
            ecu.engine_running = True
            assert wait_for(lambda: not power.sleeping)
            assert wait_for(lambda: query(sock, "010C") == "41 0C 2E E0\r\r>")
    finally:
        stop.set()
        poller.stop()
        poller.join(timeout=5)
