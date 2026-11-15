"""Command line.

    open-entity-gateway check  --config gateway.yml
    open-entity-gateway serve  --config gateway.yml [--listen 127.0.0.1:5001] [--path /records]
    open-entity-gateway replay --config gateway.yml RECORDS.jsonl [--shift-to-now] [--rate N]
    open-entity-gateway zmq    --config gateway.yml --connect tcp://127.0.0.1:12000 [--topic WX/Position]
    open-entity-gateway print  --mapping mappings --adapter cot RECORDS.jsonl

`print` writes each payload an adapter would send to standard output and
sends nothing; it needs no destination.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .adapter import ConfigError, available_adapters
from .gateway import REFUSED, Destination, Gateway, gateway_from_config, prepare_mappings
from .inputs.replay import ReplayError, paced, read_records, shift_to
from .mapping import MappingError, select
from .record import RecordError


def _records(args: argparse.Namespace) -> Iterable[dict[str, Any]]:
    stream = sys.stdin if args.file == "-" else open(args.file, encoding="utf-8")
    records: Iterable[dict[str, Any]] = read_records(stream, args.file)
    if args.shift_to_now:
        records = shift_to(iter(records), time.time_ns())
    if getattr(args, "rate", 0):
        records = paced(iter(records), args.rate)
    return records


def _run(gateway: Gateway, records: Iterable[dict[str, Any]]) -> int:
    gateway.start()
    try:
        for record in records:
            gateway.handle(record)
    except KeyboardInterrupt:
        pass
    finally:
        gateway.close()
    print("open-entity-gateway: " + gateway.summary(), file=sys.stderr)
    failed = sum(d.counts.failed + d.counts.dropped for d in gateway.destinations)
    return 1 if gateway.outcomes[REFUSED] or failed else 0


def _print(args: argparse.Namespace) -> int:
    known = available_adapters()
    if args.adapter not in known:
        raise ConfigError(f"adapter {args.adapter!r} is not installed; installed: {sorted(known)}")
    adapter = known[args.adapter]({"name": f"print-{args.adapter}", "adapter": args.adapter, "print": True})
    mappings = prepare_mappings(args.mapping, [adapter])
    refused = 0
    for record in _records(args):
        mapping = select(mappings, record)
        if mapping is None:
            continue
        try:
            track = mapping.track(record)
            payload = adapter.encode(track, record, mapping.parsed.get(adapter.name))
        except RecordError as error:
            print(f"open-entity-gateway: refused {mapping.topic}: {error}", file=sys.stderr)
            refused += 1
            continue
        print(adapter.describe(payload), flush=True)
    return 1 if refused else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="open-entity-gateway",
        description="Publish first-bite records as tracks to the destinations a configuration lists.",
    )
    parser.add_argument("--verbose", action="store_true", help="log every track")
    commands = parser.add_subparsers(dest="command", required=True)

    def with_config(name: str, text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--config", type=Path, required=True, help="the gateway configuration (YAML)")
        return sub

    with_config("check", "load the configuration and every mapping file, connect to nothing")
    serve = with_config("serve", "listen for the POSTs of first-bite's rest sink")
    serve.add_argument("--listen", default="127.0.0.1:5001", metavar="HOST:PORT")
    serve.add_argument("--path", default="/records")
    replay = with_config("replay", "read records from a JSON-lines file, or - for standard input")
    replay.add_argument("file")
    replay.add_argument(
        "--shift-to-now", action="store_true", help="move capture times so the first record is now, keeping the gaps"
    )
    replay.add_argument("--rate", type=float, default=0, metavar="N", help="records per second")
    zmq = with_config("zmq", "subscribe to first-bite's ZeroMQ publisher socket (needs the zmq extra)")
    zmq.add_argument("--connect", required=True, metavar="URL")
    zmq.add_argument("--topic", action="append", default=[], help="a topic prefix; may be repeated")
    zmq.add_argument("--encoding", choices=("json", "cbor"), default="json")
    show = commands.add_parser("print", help="write the payloads an adapter would send; send nothing")
    show.add_argument("--mapping", action="append", type=Path, required=True, metavar="PATH")
    show.add_argument("--adapter", required=True)
    show.add_argument("file")
    show.add_argument("--shift-to-now", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        if args.command == "print":
            return _print(args)
        gateway = gateway_from_config(args.config)
        if args.command == "check":
            for mapping in gateway.mappings:
                print(f"mapping {mapping.name}: {mapping.topic}")
            for destination in gateway.destinations:
                print(f"destination {destination.adapter.label}: {destination.adapter.name}")
            return 0
        if args.command == "replay":
            return _run(gateway, _records(args))
        if args.command == "zmq":
            from .inputs.zeromq import records

            return _run(gateway, records(args.connect, args.topic, args.encoding))
        return _serve(gateway, args)
    except (ConfigError, MappingError, ReplayError, OSError) as error:
        print(f"open-entity-gateway: {error}", file=sys.stderr)
        return 2


def _serve(gateway: Gateway, args: argparse.Namespace) -> int:
    from .inputs.http import make_server

    host, _, port = args.listen.rpartition(":")
    if not host or not port.isdigit():
        raise ConfigError("--listen takes HOST:PORT")
    server = make_server(gateway, host.strip("[]"), int(port), args.path)
    gateway.start()
    print(f"open-entity-gateway: listening on http://{args.listen}{args.path}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        gateway.close()
        print("open-entity-gateway: " + gateway.summary(), file=sys.stderr)
    return 0


__all__ = ["Destination", "main"]
