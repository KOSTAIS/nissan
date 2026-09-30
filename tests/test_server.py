import socket
import threading
import time

from consult2elm.data import DataStore, EngineData
from consult2elm.server import ElmServer

RUNNING = EngineData(rpm=750, coolant_c=70, speed_kmh=0, battery_v=14.1)


class FakeConn:
    def __init__(self):
        self.shut = False

    def shutdown(self, how):
        self.shut = True

    def close(self):
        pass


def live_store():
    store = DataStore()
    store.update(RUNNING, valid_for=60)
    return store


def test_reconnect_from_same_device_closes_stale_connection():
    server = ElmServer(DataStore(), transport="bluetooth")
    old = server._register(FakeConn(), ("38:8A:21:46:B5:2E", 1))
    other = server._register(FakeConn(), ("AA:BB:CC:DD:EE:FF", 1))
    server._register(FakeConn(), ("38:8A:21:46:B5:2E", 1))
    assert old.conn.shut
    assert not other.conn.shut
    assert "38:8A:21:46:B5:2E" in server.client_summary()


def test_silent_client_is_disconnected_while_engine_data_live():
    server = ElmServer(live_store(), transport="bluetooth", idle_disconnect=5)
    client = server._register(FakeConn(), ("38:8A:21:46:B5:2E", 1))
    server._check_idle()                       # engine data becomes live now
    server._live_since -= 10
    client.last_request = time.monotonic() - 10
    server._check_idle()
    assert client.conn.shut


def test_silent_client_kept_when_ignition_off():
    # Seen in the car: with the ignition off the tracker connects and waits
    # silently. Dropping that link meant it did not come back.
    server = ElmServer(DataStore(), transport="bluetooth", idle_disconnect=5)
    client = server._register(FakeConn(), ("38:8A:21:46:B5:2E", 1))
    client.connected_at -= 600
    server._check_idle()
    assert not client.conn.shut


def test_engine_start_restarts_the_silence_timer():
    server = ElmServer(live_store(), transport="bluetooth", idle_disconnect=5)
    client = server._register(FakeConn(), ("38:8A:21:46:B5:2E", 1))
    client.connected_at -= 600                 # waited silently with ignition off
    server._check_idle()                       # engine just started
    assert not client.conn.shut


def test_active_client_is_kept():
    server = ElmServer(live_store(), transport="bluetooth", idle_disconnect=5)
    client = server._register(FakeConn(), ("38:8A:21:46:B5:2E", 1))
    server._check_idle()
    server._live_since -= 10
    client.last_request = time.monotonic() - 1
    server._check_idle()
    assert not client.conn.shut


def test_idle_disconnect_over_real_socket():
    server = ElmServer(live_store(), transport="tcp", tcp_host="127.0.0.1", tcp_port=0, idle_disconnect=1.5)
    server.listen()
    stop = threading.Event()
    threading.Thread(target=server.serve_forever, args=(stop,), daemon=True).start()
    try:
        with socket.create_connection(server.address, timeout=5) as sock:
            sock.sendall(b"ATE0\r")
            assert sock.recv(64).endswith(b">")
            assert sock.recv(64) == b""          # server closed the silent link
        deadline = time.monotonic() + 3
        while server.client_count and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.client_count == 0
    finally:
        stop.set()
