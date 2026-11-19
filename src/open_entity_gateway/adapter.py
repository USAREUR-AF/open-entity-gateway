"""The adapter interface, and how adapters are found.

An adapter writes a track in one interface's form and delivers it to one
destination. The core knows no interface: it hands each destination's adapter
the track and the mapping's block for that adapter.

Adapters are found through the entry-point group `open_entity_gateway.adapters`.
The open-standard adapters ship with this package; a vendor adapter is a
separate package that adds an entry point to the same group.
"""

from __future__ import annotations

import abc
from importlib.metadata import entry_points
from typing import Any, ClassVar

from .mapping import Mapping, MappingError, Track

GROUP = "open_entity_gateway.adapters"

# Blocks of the optional vendor adapters this project publishes. A mapping
# file may carry them when the adapter is not installed; they are then unused.
VENDOR_BLOCKS = frozenset({"lattice", "picogrid_ecn"})


class ConfigError(ValueError):
    """The gateway or destination configuration is wrong."""


class DeliveryError(RuntimeError):
    """A destination did not take a payload. Carries no credential."""


class Adapter(abc.ABC):
    """One destination.

    The gateway calls, in order: `parse_block` once per mapping file when the
    files are loaded; `start` once; then for every record a mapping matches,
    `encode` on every destination first, and `send` on each only when every
    `encode` succeeded; `close` once at the end.
    """

    #: The adapter's name in a destination's `adapter:` key.
    name: ClassVar[str]
    #: The key of this adapter's block in a mapping file, or None for none.
    block: ClassVar[str | None] = None
    #: True when the adapter needs a block in every mapping it is given.
    block_required: ClassVar[bool] = False

    def __init__(self, destination: dict[str, Any]):
        self.destination = destination
        self.label = str(destination.get("name") or self.name)

    @classmethod
    def parse_block(cls, block: Any, mapping: Mapping) -> Any:
        """Check this adapter's block of one mapping file and return what
        `encode` will be given. Raise MappingError naming the file and key."""
        if block is not None:
            raise MappingError(f"{mapping.name}: {cls.block}: this adapter takes no block")
        return None

    def block_for(self, mapping: Mapping) -> Any:
        """What `parse_block` returned for this adapter in `mapping`: the
        block `encode` is given."""
        return mapping.parsed.get(self.name)

    def prepare(self, mappings: list[Mapping]) -> None:
        """Called once every mapping is loaded and its block parsed, before
        `start`: for an adapter that declares in advance what it will send."""

    def start(self) -> None:
        """Open what the destination needs: a socket, a session."""

    @abc.abstractmethod
    def encode(self, track: Track, record: dict[str, Any], block: Any) -> Any:
        """The payload for one track. Pure: sends nothing. Raises RecordError
        naming the field when the record cannot fill what the block asks."""

    @abc.abstractmethod
    def send(self, payload: Any) -> None:
        """Deliver one payload. Raises DeliveryError."""

    def describe(self, payload: Any) -> str:
        """The payload as one line of text, for `--dry-run`."""
        return payload.decode() if isinstance(payload, bytes) else str(payload)

    def close(self) -> None:
        """Deliver what is queued, then release the destination."""


def require(destination: dict[str, Any], key: str, kind: type | tuple[type, ...] = str) -> Any:
    if key not in destination:
        raise ConfigError(f"destination {destination.get('name')!r}: {key} is required")
    value = destination[key]
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
        raise ConfigError(f"destination {destination.get('name')!r}: {key} has the wrong type")
    return value


def builtin_adapters() -> dict[str, type[Adapter]]:
    from .adapters.asterix import AsterixCat062
    from .adapters.cot import CursorOnTarget
    from .adapters.fabric import FabricPublisher
    from .adapters.sapient import SapientEdgeNode

    return {cls.name: cls for cls in (CursorOnTarget, SapientEdgeNode, AsterixCat062, FabricPublisher)}


def available_adapters() -> dict[str, type[Adapter]]:
    """The built-in adapters and those installed packages add."""
    found = builtin_adapters()
    for point in entry_points(group=GROUP):
        if point.name in found:
            continue
        cls = point.load()
        if not (isinstance(cls, type) and issubclass(cls, Adapter)):
            raise ConfigError(f"entry point {point.name!r} in {GROUP} is not an Adapter")
        found[point.name] = cls
    return found
