"""Records and Canonical Track v2 objects from the goat fabric (Zenoh).

The input subscribes only to the key expressions its owner names. A key
expression made only of wildcards would match the whole keyspace, which goat
keeps for a named grant, so it is refused. What the gateway may read is
decided by the fabric's access control, never here: connection, TLS and
credentials come from the Zenoh configuration file the owner passes. Each
sample's payload is one record or one track, as JSON or CBOR. Needs the
`zenoh` extra (eclipse-zenoh).
"""

from __future__ import annotations

import json
import logging
import queue
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

log = logging.getLogger("open_entity_gateway")


class KeyExpressionError(ValueError):
    """A key expression the input refuses."""


def check_keys(keys: list[str]) -> list[str]:
    """The key expressions, or KeyExpressionError naming the one refused."""
    if not keys:
        raise KeyExpressionError("name at least one key expression to subscribe to (--key); there is no default")
    for key in keys:
        chunks = key.split("/")
        if not key or any(c == "" for c in chunks):
            raise KeyExpressionError(f"{key!r} is not a key expression")
        if all(c in ("*", "**") for c in chunks):
            raise KeyExpressionError(
                f"{key!r} is made only of wildcards and would match the whole keyspace; "
                "name the part of the fabric the tracks are published on"
            )
    return keys


def decoder(encoding: str) -> Callable[[bytes], Any]:
    if encoding == "cbor":
        import cbor2

        return cbor2.loads
    return json.loads


def records(
    keys: list[str],
    config: Path | None = None,
    connect: list[str] | None = None,
    encoding: str = "json",
    session: Any = None,
) -> Iterator[dict[str, Any]]:
    """Yield each record or track published on `keys`. `session` is an open
    Zenoh session to use instead of opening one (tests)."""
    check_keys(keys)
    try:
        import zenoh
    except ImportError:
        raise SystemExit("the Zenoh input needs eclipse-zenoh: pip install 'open-entity-gateway[zenoh]'") from None
    decode = decoder(encoding)
    owned = session is None
    if owned:
        conf = zenoh.Config.from_file(str(config)) if config else zenoh.Config()
        if connect:
            conf.insert_json5("connect/endpoints", json.dumps(connect))
        session = zenoh.open(conf)
    inbox: queue.Queue[bytes] = queue.Queue(maxsize=10_000)

    def take(sample: Any) -> None:
        try:
            inbox.put_nowait(sample.payload.to_bytes())
        except queue.Full:
            log.warning("Zenoh: input queue full, a sample was dropped")

    subscribers = [session.declare_subscriber(key, take) for key in keys]
    try:
        while True:
            payload = inbox.get()
            try:
                value = decode(payload)
            except Exception:  # json and cbor2 raise several kinds for a payload that is not a record
                log.warning("Zenoh: a sample that is not a record or a track, skipped")
                continue
            if isinstance(value, dict):
                yield value
    finally:
        for subscriber in subscribers:
            subscriber.undeclare()
        if owned:
            session.close()
