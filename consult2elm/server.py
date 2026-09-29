"""Bluetooth SPP (RFCOMM) / TCP server that exposes the ELM327 emulator."""

from __future__ import annotations

import logging
import socket
import threading

from .data import DataStore
from .elm327 import Elm327

log = logging.getLogger(__name__)


class ElmServer:
    def __init__(
        self,
        store: DataStore,
        transport: str = "bluetooth",
        channel: int = 1,
        tcp_host: str = "0.0.0.0",
        tcp_port: int = 35000,
    ):
        if transport not in ("bluetooth", "tcp"):
            raise ValueError("transport must be 'bluetooth' or 'tcp'")
        self._store = store
        self._transport = transport
        self._channel = channel
        self._tcp_addr = (tcp_host, tcp_port)
        self._sock = None
        self.address = None
        self.client_count = 0
        self.request_count = 0
        self._count_lock = threading.Lock()

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
                try:
                    conn, peer = self._sock.accept()
                except socket.timeout:
                    continue
                threading.Thread(
                    target=self._handle, args=(conn, peer, stop), name=f"elm-{peer}", daemon=True
                ).start()
        finally:
            self._sock.close()

    def _handle(self, conn: socket.socket, peer, stop: threading.Event) -> None:
        log.info("OBD client connected: %s", peer)
        with self._count_lock:
            self.client_count += 1
        elm = Elm327(self._store)
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
                self.request_count += data.count(b"\r")
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
            conn.close()
            with self._count_lock:
                self.client_count -= 1
            log.info("OBD client disconnected: %s", peer)
