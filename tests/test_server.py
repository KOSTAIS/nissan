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
