"""Bluetooth SPP (RFCOMM) / TCP server that exposes the ELM327 emulator."""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .data import DataStore
from .elm327 import Elm327

log = logging.getLogger(__name__)

IDLE_WARNING = 60.0  # seconds without a request before a client is reported as silent


@dataclass(eq=False)
class ClientInfo:
    peer: tuple
    conn: socket.socket
    connected_at: float = field(default_factory=time.monotonic)
    last_request: Optional[float] = None
    requests: int = 0
    idle_warned: bool = False

    @property
    def name(self) -> str:
        return str(self.peer[0])

    def idle_for(self, now: float) -> float:
        return now - (self.last_request or self.connected_at)


class ElmServer:
    def __init__(
        self,
        store: DataStore,
        transport: str = "bluetooth",
        channel: int = 1,
        tcp_host: str = "0.0.0.0",
        tcp_port: int = 35000,
        vin: str = "",
        idle_disconnect: float = 120.0,
    ):
        if transport not in ("bluetooth", "tcp"):
            raise ValueError("transport must be 'bluetooth' or 'tcp'")
        self._store = store
        self._transport = transport
        self._channel = channel
        self._tcp_addr = (tcp_host, tcp_port)
        self._vin = vin
        self._idle_disconnect = idle_disconnect
        self._live_since: Optional[float] = None  # engine data live since (monotonic)
        self._sock = None
        self.address = None
        self.request_count = 0
        self._clients: List[ClientInfo] = []
        self._lock = threading.Lock()

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def client_summary(self) -> str:
        """e.g. '38:8A:21:46:B5:2E (412 req, last 0s ago)'."""
        now = time.monotonic()
        with self._lock:
            parts = [
                f"{c.name} ({c.requests} req, last {c.idle_for(now):.0f}s ago)" for c in self._clients
            ]
        return ", ".join(parts) or "none"

    def listen(self) -> None:
        if self._transport == "bluetooth":
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            sock.bind((socket.BDADDR_ANY, self._channel))
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(self._tcp_addr)
        sock.listen(2)
        sock.settimeout(1.0)
        self._sock = sock
        self.address = sock.getsockname()
        log.info("ELM327 emulator listening on %s %s", self._transport, self.address)
        if self._transport == "bluetooth":
            log.info("Waiting for an OBD client (tracker or phone app) to connect over Bluetooth")

    def serve_forever(self, stop: threading.Event) -> None:
        if self._sock is None:
            self.listen()
        try:
            while not stop.is_set():
                self._check_idle()
                try:
                    conn, peer = self._sock.accept()
                except socket.timeout:
                    continue
                client = self._register(conn, peer)
                threading.Thread(
                    target=self._handle, args=(client, stop), name=f"elm-{peer}", daemon=True
                ).start()
        finally:
            self._sock.close()

    def _register(self, conn: socket.socket, peer) -> ClientInfo:
        client = ClientInfo(peer=peer, conn=conn)
        with self._lock:
            stale = []
            if self._transport == "bluetooth":
                # A Bluetooth device has one link to us. If it connects again,
                # the old connection is dead (e.g. tracker rebooted, link lost)
                # even if the socket has not noticed yet.
                stale = [c for c in self._clients if c.peer[0] == peer[0]]
            self._clients.append(client)
        for old in stale:
            log.info("OBD client %s reconnected, closing its previous connection", old.name)
            self._shutdown(old)
        return client

    def _check_idle(self) -> None:
        """Warn about, and optionally drop, clients that stop polling.

        Silence only counts while the engine data is live: with the ignition
        off the FMB130 keeps the link open without asking anything, and that
        idle link must be kept for when the ignition comes back on.
        """
        now = time.monotonic()
        if self._store.current() is None:
            self._live_since = None
            return
        if self._live_since is None:
            self._live_since = now
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            idle = now - max(c.last_request or c.connected_at, self._live_since)
            if self._idle_disconnect and idle > self._idle_disconnect:
                # The FMB130 sometimes ends its session with ATPC and then keeps
                # the link open without asking anything, even with the engine
                # running. Dropping the link makes it reconnect and poll again.
                log.warning("OBD client %s silent for %.0f s with the engine data live, "
                            "closing the connection so it reconnects", c.name, idle)
                c.last_request = now  # log once; the handler thread cleans up
                self._shutdown(c)
            elif not c.idle_warned and idle > IDLE_WARNING:
                c.idle_warned = True
                log.warning("OBD client %s sent no request for %.0f s although engine data is live",
                            c.name, idle)

    @staticmethod
    def _shutdown(client: ClientInfo) -> None:
        """Wake the client's handler thread; it closes the socket itself."""
        try:
            client.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _handle(self, client: ClientInfo, stop: threading.Event) -> None:
        conn, peer = client.conn, client.peer
        log.info("OBD client connected: %s", peer)
        elm = Elm327(self._store, vin=self._vin)
        seen = set()
        conn.settimeout(1.0)
        try:
            while not stop.is_set():
                try:
                    data = conn.recv(256)
                except socket.timeout:
                    continue
                if not data:
                    break
                log.debug("<- %r", data)
                reply = elm.feed(data)
                lines = data.count(b"\r")
                if lines:
                    client.last_request = time.monotonic()
                    client.requests += lines
                    client.idle_warned = False
                    with self._lock:
                        self.request_count += lines
                command = data.strip().upper()
                if command and command not in seen and len(seen) < 100:
                    # Record what the tracker asks for even without --debug.
                    seen.add(command)
                    log.info("Client request %r -> reply %r", data, reply)
                if reply:
                    log.debug("-> %r", reply)
                    conn.sendall(reply)
        except OSError as exc:
            log.info("OBD client %s error: %s", peer, exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)
            log.info("OBD client disconnected: %s (%d requests)", peer, client.requests)
