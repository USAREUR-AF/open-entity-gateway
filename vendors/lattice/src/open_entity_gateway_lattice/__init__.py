"""Lattice: each track is published as an entity with
`client.entities.publish_entity` of the Lattice SDK for Python.

The SDK (`anduril-lattice-sdk`, imported as `anduril`) is not part of this
project and is not a dependency of this package. It is imported only here,
when a destination is started, from the user's own installation. See
vendors/lattice/README.md for its licence before installing it.

Credentials come from the environment, under the names the vendor's
documentation uses: LATTICE_ENDPOINT, ENVIRONMENT_TOKEN and, for a Sandboxes
environment only, SANDBOXES_TOKEN. None is in the gateway's configuration.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable
from typing import Any

from open_entity_gateway.adapter import Adapter, ConfigError, DeliveryError
from open_entity_gateway.mapping import Mapping, MappingError, Track, Value, constrained_value
from open_entity_gateway.record import NANOS, RecordError, rfc3339_z

SDK_HELP = (
    "the Lattice SDK is not installed. It is not open source: installing or using it accepts its vendor's "
    "licence agreement (https://github.com/anduril/lattice-sdk-python/blob/master/LICENSE). If you accept it, "
    "run `pip install anduril-lattice-sdk`. `open-entity-gateway print --adapter lattice` needs no SDK."
)
TEMPLATES = ("TEMPLATE_TRACK", "TEMPLATE_ASSET")
# CoT affiliation to the Entities API's disposition. Joker and faker are
# exercise roles: they keep the hostile or suspect disposition they act and
# set indicators.exercise.
DISPOSITION = {
    "friend": "DISPOSITION_FRIENDLY",
    "assumed-friend": "DISPOSITION_ASSUMED_FRIENDLY",
    "neutral": "DISPOSITION_NEUTRAL",
    "suspect": "DISPOSITION_SUSPICIOUS",
    "hostile": "DISPOSITION_HOSTILE",
    "pending": "DISPOSITION_PENDING",
    "unknown": "DISPOSITION_UNKNOWN",
    "joker": "DISPOSITION_SUSPICIOUS",
    "faker": "DISPOSITION_HOSTILE",
}
ENVIRONMENT = {
    "air": "ENVIRONMENT_AIR",
    "ground": "ENVIRONMENT_LAND",
    "sea-surface": "ENVIRONMENT_SURFACE",
    "subsurface": "ENVIRONMENT_SUB_SURFACE",
    "space": "ENVIRONMENT_SPACE",
}
# The longest expiry the Entities API accepts is under 30 days.
MAX_EXPIRY_MINUTES = 30 * 24 * 60 - 1
_KEYS = {"name", "adapter", "topics", "queue", "print"}
_BLOCK_KEYS = {"entity_id", "template", "platform_type", "specific_type", "description", "indicators", "provenance"}
_PROVENANCE = {"integration_name", "data_type", "source_id", "source_description"}


class Block:
    def __init__(self, raw: dict[str, Any], where: str):
        self.entity_id = Value(raw["entity_id"], f"{where}.entity_id") if "entity_id" in raw else None
        self.template = (
            constrained_value(raw, "template", where, TEMPLATES)
            if "template" in raw
            else Value("TEMPLATE_TRACK", where)
        )
        self.platform_type = Value(raw["platform_type"], f"{where}.platform_type") if "platform_type" in raw else None
        self.specific_type = Value(raw["specific_type"], f"{where}.specific_type") if "specific_type" in raw else None
        self.description = Value(raw["description"], f"{where}.description") if "description" in raw else None
        indicators = raw.get("indicators", {})
        if (
            not isinstance(indicators, dict)
            or set(indicators) - {"simulated", "exercise"}
            or not all(isinstance(v, bool) for v in indicators.values())
        ):
            raise MappingError(f"{where}.indicators: true or false for simulated and exercise")
        self.indicators: dict[str, bool] = dict(indicators)
        provenance = raw.get("provenance", {})
        if not isinstance(provenance, dict) or set(provenance) - _PROVENANCE:
            raise MappingError(f"{where}.provenance: takes {sorted(_PROVENANCE)}")
        self.provenance = {k: Value(v, f"{where}.provenance.{k}") for k, v in provenance.items()}


class Lattice(Adapter):
    name = "lattice"
    block = "lattice"

    def __init__(
        self,
        destination: dict[str, Any],
        environ: dict[str, str] | None = None,
        httpx_client: Any = None,
        clock: Callable[[], dt.datetime] | None = None,
    ):
        super().__init__(destination)
        unknown = set(destination) - _KEYS
        if unknown:
            raise ConfigError(f"destination {self.label!r}: unknown key {sorted(unknown)[0]!r}")
        self.printing = bool(destination.get("print"))
        self.environ = os.environ if environ is None else environ
        self.httpx_client = httpx_client
        self.clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self.client: Any = None
        self._entity: Any = None
        self._api_error: Any = None

    @classmethod
    def parse_block(cls, block: Any, mapping: Mapping) -> Block:
        where = f"{mapping.name}: lattice"
        if block is None:
            block = {}
        if not isinstance(block, dict) or set(block) - _BLOCK_KEYS:
            raise MappingError(f"{where}: takes {sorted(_BLOCK_KEYS)}")
        if not 0 < mapping.stale_minutes <= MAX_EXPIRY_MINUTES:
            raise MappingError(f"{mapping.name}: stale_minutes: Lattice takes an expiry above 0 and under 30 days")
        return Block(block, where)

    def encode(self, track: Track, record: dict[str, Any], block: Block | None) -> dict[str, Any]:
        """The entity as the JSON object the Entities API documents."""
        block = block or Block({}, "lattice")
        source = track.source
        data_type = "/".join(p for p in (source.dictionary, source.subsystem, source.message_type) if p)
        provenance = {
            "integrationName": block.provenance["integration_name"].resolve(record)
            if "integration_name" in block.provenance
            else "open-entity-gateway",
            "dataType": block.provenance["data_type"].resolve(record) if "data_type" in block.provenance else data_type,
            "sourceId": block.provenance["source_id"].resolve(record) if "source_id" in block.provenance else track.uid,
            # Lattice applies an update only when this is later than the one it holds.
            "sourceUpdateTime": rfc3339_z(track.time_ns),
        }
        if "source_description" in block.provenance:
            provenance["sourceDescription"] = block.provenance["source_description"].resolve(record)
        position: dict[str, float] = {"latitudeDegrees": track.latitude, "longitudeDegrees": track.longitude}
        if track.hae is not None:
            position["altitudeHaeMeters"] = track.hae
        expiry = self.clock() + dt.timedelta(minutes=(track.stale_ns - track.time_ns) / NANOS / 60)
        entity: dict[str, Any] = {
            "entityId": block.entity_id.resolve(record) if block.entity_id else track.uid,
            "isLive": True,
            "expiryTime": rfc3339_z(int(expiry.timestamp()) * NANOS + expiry.microsecond * 1000),
        }
        if block.description is not None:
            entity["description"] = block.description.resolve(record)
        if track.name is not None:
            entity["aliases"] = {"name": track.name}
        entity["location"] = {"position": position}
        entity["milView"] = {
            "disposition": DISPOSITION.get(track.affiliation, "DISPOSITION_UNKNOWN"),
            "environment": ENVIRONMENT.get(track.dimension, "ENVIRONMENT_UNKNOWN"),
        }
        ontology = {"template": block.template.resolve(record)}
        if block.platform_type is not None:
            ontology["platformType"] = block.platform_type.resolve(record)
        if block.specific_type is not None:
            ontology["specificType"] = block.specific_type.resolve(record)
        entity["ontology"] = ontology
        entity["provenance"] = provenance
        indicators = dict(block.indicators)
        if track.affiliation in ("joker", "faker"):
            indicators["exercise"] = True
        if indicators:
            entity["indicators"] = indicators
        if not entity["entityId"]:
            raise RecordError("lattice.entity_id is empty")
        return entity

    def describe(self, payload: dict[str, Any]) -> str:
        import json

        return json.dumps(payload, separators=(",", ":"))

    def start(self) -> None:
        if self.printing:
            return
        endpoint = self.environ.get("LATTICE_ENDPOINT", "").strip()
        token = self.environ.get("ENVIRONMENT_TOKEN", "").strip()
        if not endpoint or not token:
            raise ConfigError(f"destination {self.label!r}: set LATTICE_ENDPOINT and ENVIRONMENT_TOKEN")
        if "://" in endpoint or "/" in endpoint:
            raise ConfigError(f"destination {self.label!r}: LATTICE_ENDPOINT is a host name only")
        try:
            from anduril import Lattice as Client
            from anduril.core.api_error import ApiError
            from anduril.types.entity import Entity
        except ImportError:
            raise ConfigError(SDK_HELP) from None
        options: dict[str, Any] = {"base_url": f"https://{endpoint}", "token": lambda: token}
        sandboxes = self.environ.get("SANDBOXES_TOKEN", "").strip()
        if sandboxes:
            options["headers"] = {"anduril-sandbox-authorization": f"Bearer {sandboxes}"}
        if self.httpx_client is not None:
            options["httpx_client"] = self.httpx_client
        self.client = Client(**options)
        self._entity, self._api_error = Entity, ApiError

    def send(self, payload: dict[str, Any]) -> None:
        assert self.client is not None
        try:
            model = self._entity.model_validate(payload)
        except ValueError as error:
            raise DeliveryError(f"the SDK does not accept the entity: {error.__class__.__name__}") from None
        components = {name: getattr(model, name) for name in model.model_fields_set}
        try:
            self.client.entities.publish_entity(**components)
        except self._api_error as error:
            raise DeliveryError(f"Lattice answered {error.status_code}") from None
        except Exception as error:  # a transport failure: name its kind, never its text (it may hold a token)
            raise DeliveryError(f"request failed: {type(error).__name__}") from None
