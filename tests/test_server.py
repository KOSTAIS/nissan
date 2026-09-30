from consult2elm.data import DataStore
from consult2elm.server import ElmServer


class FakeConn:
    def __init__(self):
        self.closed = False

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


def test_reconnect_from_same_device_closes_stale_connection():
    server = ElmServer(DataStore(), transport="bluetooth")
    old = server._register(FakeConn(), ("38:8A:21:46:B5:2E", 1))
    other = server._register(FakeConn(), ("AA:BB:CC:DD:EE:FF", 1))
    server._register(FakeConn(), ("38:8A:21:46:B5:2E", 1))
    assert old.conn.closed
    assert not other.conn.closed
    assert "38:8A:21:46:B5:2E" in server.client_summary()


def test_silent_client_is_disconnected():
    import time
    server = ElmServer(DataStore(), transport="bluetooth", idle_disconnect=5)
    client = server._register(FakeConn(), ("38:8A:21:46:B5:2E", 1))
    client.last_request = time.monotonic() - 10
    server._check_idle()
    assert client.conn.closed


def test_active_client_is_kept():
    import time
    server = ElmServer(DataStore(), transport="bluetooth", idle_disconnect=5)
    client = server._register(FakeConn(), ("38:8A:21:46:B5:2E", 1))
    client.last_request = time.monotonic() - 1
    server._check_idle()
    assert not client.conn.closed


def test_idle_disconnect_over_real_socket():
    import socket
    import threading
    import time
    server = ElmServer(DataStore(), transport="tcp", tcp_host="127.0.0.1", tcp_port=0, idle_disconnect=1.5)
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
