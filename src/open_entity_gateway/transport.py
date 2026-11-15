"""Sockets the adapters share: a datagram sender and a reconnecting stream.

A stream reconnects after 1 s, then 2 s, 4 s and so on up to 30 s, as
first-bite's tcp sink does. While it is away, payloads wait in the
destination's queue; a full queue drops and counts.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import ssl
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .adapter import ConfigError, DeliveryError

log = logging.getLogger("open_entity_gateway")

MAX_BACKOFF = 30.0


def split_address(text: Any, label: str) -> tuple[str, int]:
    if not isinstance(text, str):
        raise ConfigError(f"destination {label!r}: address is HOST:PORT")
    host, _, port = text.rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ConfigError(f"destination {label!r}: address is HOST:PORT, got {text!r}")
    return host, int(port)


def tls_context(spec: Any, label: str) -> ssl.SSLContext | None:
    """A client TLS context from a destination's `tls:` table: `ca_file` (the
    only authorities trusted), optional `client_cert_file` and
    `client_key_file`."""
    if spec is None:
        return None
    if not isinstance(spec, dict) or set(spec) - {"ca_file", "client_cert_file", "client_key_file", "server_name"}:
        raise ConfigError(f"destination {label!r}: tls takes ca_file, client_cert_file, client_key_file, server_name")
    if "ca_file" not in spec:
        raise ConfigError(f"destination {label!r}: tls.ca_file is required: the server is checked against it only")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if "client_key_file" in spec and "client_cert_file" not in spec:
        raise ConfigError(f"destination {label!r}: tls.client_key_file needs tls.client_cert_file")
    for key in ("ca_file", "client_cert_file", "client_key_file"):
        if key in spec:
            try:
                Path(spec[key]).read_bytes()
            except OSError as error:
                raise ConfigError(
                    f"destination {label!r}: tls.{key}: cannot read {spec[key]}: {error.strerror}"
                ) from None
    try:
        context.load_verify_locations(cafile=spec["ca_file"])
        if "client_cert_file" in spec:
            context.load_cert_chain(spec["client_cert_file"], spec.get("client_key_file"))
    except ssl.SSLError as error:
        raise ConfigError(f"destination {label!r}: tls: {error.reason or error}") from None
    return context


class Datagrams:
    """UDP to one address: unicast, or a multicast group with `ttl` and the
    `interface` address to send from."""

    def __init__(self, destination: dict[str, Any], label: str):
        self.label = label
        self.host, self.port = split_address(destination.get("address"), label)
        self.ttl = destination.get("ttl")
        self.interface = destination.get("interface")
        self.source_port = destination.get("source_port")
        self.sock: socket.socket | None = None
        self.target: Any = None

    def open(self) -> None:
        info = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_DGRAM)[0]
        family, _, _, _, self.target = info
        sock = socket.socket(family, socket.SOCK_DGRAM)
        if self.source_port is not None:
            sock.bind(("", int(self.source_port)))
        try:
            multicast = ipaddress.ip_address(self.target[0]).is_multicast
        except ValueError:
            multicast = False
        if multicast and family == socket.AF_INET:
            if self.ttl is not None:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, int(self.ttl))
            if self.interface is not None:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(str(self.interface)))
        elif self.ttl is not None:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, int(self.ttl))
        self.sock = sock
        log.info("destination %s: sending UDP to %s:%d", self.label, *self.target[:2])

    def send(self, payload: bytes) -> None:
        if self.sock is None:
            self.open()
        assert self.sock is not None
        try:
            self.sock.sendto(payload, self.target)
        except OSError as error:
            raise DeliveryError(f"UDP send failed: {type(error).__name__}") from None

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None


class Stream:
    """One TCP connection, optionally TLS, kept open and reopened when lost."""

    def __init__(self, destination: dict[str, Any], label: str, timeout: float = 10.0):
        self.label = label
        self.host, self.port = split_address(destination.get("address"), label)
        self.context = tls_context(destination.get("tls"), label)
        tls = destination.get("tls") or {}
        self.server_name = tls.get("server_name") or self.host
        self.source_port = destination.get("source_port")
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.closing = threading.Event()
        self.reconnections = 0
        # Sent first on every new connection, after `settle` seconds.
        self.greeting: Callable[[], bytes] | None = None
        self.settle = 0.0

    def connect(self) -> socket.socket:
        backoff = 1.0
        while not self.closing.is_set():
            try:
                source = ("", int(self.source_port)) if self.source_port is not None else None
                raw = socket.create_connection((self.host, self.port), timeout=self.timeout, source_address=source)
                sock: socket.socket = raw
                if self.context is not None:
                    sock = self.context.wrap_socket(raw, server_hostname=self.server_name)
                sock.settimeout(self.timeout)
                if self.greeting is not None:
                    time.sleep(self.settle)
                    sock.sendall(self.greeting())
                log.info(
                    "destination %s: connected to %s:%d%s",
                    self.label,
                    self.host,
                    self.port,
                    ", with TLS" if self.context else "",
                )
                return sock
            except (OSError, ssl.SSLError) as error:
                log.warning(
                    "destination %s: cannot connect to %s:%d (%s); next try in %.0f s",
                    self.label,
                    self.host,
                    self.port,
                    type(error).__name__,
                    backoff,
                )
                if self.closing.wait(backoff):
                    break
                backoff = min(backoff * 2, MAX_BACKOFF)
        raise DeliveryError("closed before a connection was made")

    def send(self, payload: bytes) -> None:
        for _ in range(2):
            if self.sock is None:
                self.sock = self.connect()
            try:
                self.sock.sendall(payload)
                return
            except (OSError, ssl.SSLError) as error:
                log.warning("destination %s: connection lost (%s); reconnecting", self.label, type(error).__name__)
                self._drop()
                self.reconnections += 1
        raise DeliveryError("connection lost twice while sending")

    def _drop(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def close(self, linger: float = 2.0) -> None:
        """Close after the peer has read what was sent. Closing a socket that
        holds unread data from the peer resets the connection, and a reset
        can discard what the peer had not yet read (measured with
        OpenTAKServer over TLS). So: stop sending, read until the peer closes
        or `linger` seconds pass, then close."""
        self.closing.set()
        sock = self.sock
        if sock is not None:
            try:
                if isinstance(sock, ssl.SSLSocket):
                    sock.settimeout(linger)
                    sock = sock.unwrap()
                sock.shutdown(socket.SHUT_WR)
                sock.settimeout(0.2)
                deadline = time.monotonic() + linger
                while time.monotonic() < deadline:
                    try:
                        if not sock.recv(65536):
                            break
                    except (TimeoutError, ssl.SSLError):
                        continue
            except (OSError, ssl.SSLError, ValueError):
                pass
            try:
                sock.close()
            except OSError:
                pass
        self.sock = None
