"""One record in; at most one track out, to every destination or to none.

A Canonical Track v2 object is accepted wherever a record is: it is first
turned into a record (canonical_track.py) and then mapped like any other.

A record that no mapping matches is skipped. A record a mapping matches is
turned into a track, then every destination's adapter writes its payload. Only
when every payload was written is anything sent: a record that cannot fill
its mapping, or any adapter's block, is refused, the log names the field, and
no destination receives anything for it.

Each destination has its own queue and thread, so a slow or absent
destination holds up neither capture nor the others.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .adapter import VENDOR_BLOCKS, Adapter, ConfigError, DeliveryError, available_adapters
from .canonical_track import is_canonical_track, track_record
from .mapping import Mapping, MappingError, load_mappings, select
from .record import RecordError

log = logging.getLogger("open_entity_gateway")

SENT, SKIPPED, REFUSED = "sent", "skipped", "refused"
DEFAULT_QUEUE = 1000
_STOP = object()


@dataclass
class Counts:
    sent: int = 0
    dropped: int = 0
    failed: int = 0


@dataclass
class Destination:
    adapter: Adapter
    topics: set[str] | None
    queue_size: int
    counts: Counts = field(default_factory=Counts)
    _queue: queue.Queue[Any] = field(init=False)
    _thread: threading.Thread | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._queue = queue.Queue(maxsize=self.queue_size)

    def wants(self, mapping: Mapping) -> bool:
        return self.topics is None or mapping.topic in self.topics

    def start(self) -> None:
        self.adapter.start()
        self._thread = threading.Thread(target=self._run, name=f"deliver-{self.adapter.label}", daemon=True)
        self._thread.start()

    def put(self, payload: Any) -> None:
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self.counts.dropped += 1
            log.warning("destination %s: queue full, payload dropped", self.adapter.label)

    def _run(self) -> None:
        while True:
            payload = self._queue.get()
            if payload is _STOP:
                return
            try:
                self.adapter.send(payload)
                self.counts.sent += 1
            except DeliveryError as error:
                self.counts.failed += 1
                log.error("destination %s: not delivered: %s", self.adapter.label, error)

    def close(self, timeout: float) -> None:
        if self._thread is not None:
            self._queue.put(_STOP)
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.error("destination %s: %d payloads still queued at close", self.adapter.label, self._queue.qsize())
        self.adapter.close()


class Gateway:
    def __init__(
        self,
        mappings: list[Mapping],
        destinations: list[Destination],
        clock: Callable[[], int] = time.time_ns,
    ):
        if not destinations:
            raise ConfigError(
                "no destinations: the gateway sends only to the destinations its configuration lists, "
                "and this one lists none"
            )
        self.mappings = mappings
        self.destinations = destinations
        self.clock = clock
        self.outcomes = {SENT: 0, SKIPPED: 0, REFUSED: 0}

    def start(self) -> None:
        for destination in self.destinations:
            destination.start()

    def close(self, timeout: float = 10.0) -> None:
        for destination in self.destinations:
            destination.close(timeout)

    def handle(self, record: Any) -> str:
        outcome = self._handle(record)
        self.outcomes[outcome] += 1
        return outcome

    def _handle(self, record: Any) -> str:
        if not isinstance(record, dict):
            log.warning("refused: not a record, a JSON %s", type(record).__name__)
            return REFUSED
        if is_canonical_track(record):
            try:
                record = track_record(record)
            except RecordError as error:
                log.warning("refused a Canonical Track v2: %s; nothing sent", error)
                return REFUSED
        mapping = select(self.mappings, record)
        if mapping is None:
            return SKIPPED
        targets = [d for d in self.destinations if d.wants(mapping)]
        if not targets:
            return SKIPPED
        try:
            track = mapping.track(record, received_ns=self.clock())
            payloads = [d.adapter.encode(track, record, mapping.parsed.get(d.adapter.name)) for d in targets]
        except RecordError as error:
            log.warning("refused %s (%s): %s; nothing sent", mapping.topic, mapping.name, error)
            return REFUSED
        for destination, payload in zip(targets, payloads, strict=True):
            destination.put(payload)
        log.info("track %s from %s to %d destinations", track.uid, mapping.topic, len(targets))
        return SENT

    def summary(self) -> str:
        parts = [f"{count} {what}" for what, count in self.outcomes.items()]
        for d in self.destinations:
            parts.append(
                f"{d.adapter.label}: {d.counts.sent} delivered, {d.counts.dropped} dropped, {d.counts.failed} failed"
            )
        return "; ".join(parts)


def build_adapters(specs: list[dict[str, Any]]) -> list[tuple[Adapter, dict[str, Any]]]:
    known = available_adapters()
    built = []
    names: set[str] = set()
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise ConfigError(f"destinations[{index}]: a table")
        label = spec.get("name")
        if not isinstance(label, str) or not label:
            raise ConfigError(f"destinations[{index}]: name is required")
        if label in names:
            raise ConfigError(f"destinations: the name {label!r} is used twice")
        names.add(label)
        kind = spec.get("adapter")
        if kind not in known:
            raise ConfigError(f"destination {label!r}: adapter {kind!r} is not installed; installed: {sorted(known)}")
        built.append((known[kind](spec), spec))
    return built


def prepare_mappings(paths: list[Path], adapters: list[Adapter]) -> list[Mapping]:
    """Load the mapping files, and have each adapter check its block in each."""
    known = available_adapters()
    blocks = {cls.block for cls in known.values() if cls.block} | VENDOR_BLOCKS
    mappings = load_mappings(paths, blocks)
    for mapping in mappings:
        for adapter in adapters:
            cls = type(adapter)
            raw = mapping.blocks.get(cls.block) if cls.block else None
            if raw is None and cls.block_required:
                raise MappingError(f"{mapping.name}: the {cls.name} adapter needs a {cls.block}: block")
            if cls.name not in mapping.parsed:
                mapping.parsed[cls.name] = cls.parse_block(raw, mapping)
    for adapter in adapters:
        adapter.prepare(mappings)
    return mappings


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"{path}: {error}") from None
    if not isinstance(config, dict):
        raise ConfigError(f"{path}: a table with mappings and destinations")
    unknown = set(config) - {"mappings", "destinations"}
    if unknown:
        raise ConfigError(f"{path}: unknown key {sorted(unknown)[0]!r}")
    mappings = config.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        raise ConfigError(f"{path}: mappings: list the mapping files or directories")
    config["mappings"] = [(path.parent / str(m)).resolve() for m in mappings]
    destinations = config.get("destinations")
    if not isinstance(destinations, list) or not destinations:
        raise ConfigError(
            f"{path}: destinations: list where tracks go. There is no default destination; "
            "each entry names an adapter and an address its owner chose"
        )
    return config


def gateway_from_config(path: Path) -> Gateway:
    config = load_config(path)
    built = build_adapters(config["destinations"])
    mappings = prepare_mappings(config["mappings"], [adapter for adapter, _ in built])
    destinations = []
    for adapter, spec in built:
        topics = spec.get("topics")
        if topics is not None and (not isinstance(topics, list) or not all(isinstance(t, str) for t in topics)):
            raise ConfigError(f"destination {adapter.label!r}: topics: a list of SUBSYSTEM/MESSAGE_TYPE")
        size = spec.get("queue", DEFAULT_QUEUE)
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ConfigError(f"destination {adapter.label!r}: queue: a whole number above 0")
        destinations.append(Destination(adapter, set(topics) if topics is not None else None, size))
    return Gateway(mappings, destinations)
