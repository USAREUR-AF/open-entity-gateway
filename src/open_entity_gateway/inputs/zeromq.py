"""Records from first-bite's ZeroMQ publisher socket.

Each message is `<topic>|<record>`, the record as JSON or CBOR (first-bite
docs/record.md). Needs the `zmq` extra (pyzmq).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from typing import Any


def records(url: str, topics: list[str], encoding: str = "json") -> Iterator[dict[str, Any]]:
    try:
        import zmq
    except ImportError:
        raise SystemExit("the ZeroMQ input needs pyzmq: pip install 'open-entity-gateway[zmq]'") from None
    decode: Callable[[bytes], Any]
    if encoding == "cbor":
        import cbor2

        decode = cbor2.loads
    else:
        decode = json.loads
    context: Any = zmq.Context.instance()
    socket = context.socket(zmq.SUB)
    socket.connect(url)
    for topic in topics or [""]:
        socket.setsockopt(zmq.SUBSCRIBE, topic.encode())
    try:
        while True:
            frame = socket.recv()
            _, _, payload = frame.partition(b"|")
            try:
                record = decode(payload)
            except Exception:  # json and cbor2 raise several kinds for a frame that is not a record
                logging.getLogger("open_entity_gateway").warning("ZeroMQ: a frame that is not a record, skipped")
                continue
            if isinstance(record, dict):
                yield record
    finally:
        socket.close(linger=0)
