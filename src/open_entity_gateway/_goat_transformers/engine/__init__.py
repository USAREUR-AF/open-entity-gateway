"""goat-transformers, independent Python engine (standard library only).

Implemented from spec/ and packs/ only. Formats are data: an Engine loads
model/*.def and then packs/<format>/pack.def at start and has no format
compiled in.
"""

from .engine import Engine, Entity, Item
from .errors import (
    BadDirective,
    BadLicence,
    BadLocator,
    DuplicateFormat,
    MissingFormat,
    MissingModule,
    MissingVersion,
    MissingWire,
    UnknownWire,
    UnterminatedQuote,
    DuplicateField,
    DuplicateKind,
    NoKind,
    DistributionRefused,
    DistributionViolation,
    DuplicateKey,
    DuplicateModule,
    GTError,
    InvalidModel,
    InvalidPack,
    KeyTypeMismatch,
    MissingDistribution,
    MissingSource,
    ModuleNotUsed,
    UndeclaredKey,
    UnknownModule,
    MalformedWire,
    MissingRoot,
    TransformDecodeFailure,
    TransformEncodeFailure,
    UnknownDirective,
    UnknownFormat,
    UnknownTransform,
)
from .licence import PROFILES
from .modeldef import Derive, Key, Module, load_model_file, parse_model_text
from .packdef import Pack, load_pack, parse_pack_text

__version__ = "0.8.0"

__all__ = [
    "Engine", "Entity", "Item", "Pack", "load_pack", "parse_pack_text", "GTError", "InvalidPack",
    "MalformedWire", "MissingRoot", "TransformDecodeFailure", "TransformEncodeFailure",
    "UnknownDirective", "UnknownFormat", "UnknownTransform",
    "Key", "Module", "Derive", "load_model_file", "parse_model_text", "InvalidModel",
    "MissingDistribution", "MissingSource", "DistributionRefused", "UnknownModule",
    "DistributionViolation", "ModuleNotUsed", "UndeclaredKey", "KeyTypeMismatch",
    "DuplicateModule", "DuplicateKey",
    "NoKind", "DuplicateKind", "DuplicateField", "BadDirective", "BadLicence", "PROFILES",
    "BadLocator", "DuplicateFormat", "MissingFormat", "MissingModule", "MissingVersion", "MissingWire",
    "UnknownWire", "UnterminatedQuote",
]
