"""Octet values shared by the binary wires: hex text in (ruling 36 of 0.8),
and the `utf8` and `hex` transforms of an octet string, which the `tlv` wire
(0.13) and the UPER `octets:` locator of `bits` (0.19) both use.

A build carries this module when it has `bits`, `protobuf` or `tlv`, so that
a build with `bits` and no `tlv` has what it needs (Version 0.24, ruling 1d).
"""

import re

from .errors import MalformedWire
from .transforms import Transform

_HEX_WS = {ord(c): None for c in " \t\r\n"}
_NOT_HEX = re.compile("[^0-9a-fA-F]")
_HEX_TEXT = re.compile(r"(?:[0-9a-fA-F]{2})*\Z")


def hex_to_bytes(data, fmt):
    """Hex text -> bytes (ruling 36 of 0.8). Space, tab, carriage return and
    line feed are ignored wherever they stand; any other character that is not
    one of `0-9a-fA-F` (a sign, a digit of another script, any other white
    space) and an odd number of digits are `malformed-wire`."""
    cleaned = data.translate(_HEX_WS)
    bad = _NOT_HEX.search(cleaned)
    if bad is not None:
        raise MalformedWire("input is not hexadecimal: character %r" % bad.group(), format=fmt)
    if len(cleaned) % 2 != 0:
        raise MalformedWire("hex input has an odd number of digits", format=fmt)
    return bytes.fromhex(cleaned)


class TlvTransform(Transform):
    kind = "tlv"
    empty_ok = False  # an empty value is absent (ruling 1); `utf8` and `hex` read it as ""


class Utf8T(TlvTransform):
    name = "utf8"
    yields = "string"
    empty_ok = True

    def decode(self, raw):
        return bytes(raw).decode("utf-8")  # strict: invalid UTF-8 is a ValueError

    def encode(self, value):
        if not isinstance(value, str):
            raise ValueError("utf8 transform needs a str, got %s" % type(value).__name__)
        return value.encode("utf-8")


class HexT(TlvTransform):
    name = "hex"
    yields = "string"
    empty_ok = True

    def decode(self, raw):
        return bytes(raw).hex()

    def encode(self, value):
        if not isinstance(value, str) or not value.isascii() or _HEX_TEXT.match(value) is None:
            raise ValueError("hex: %r is not an even number of hex digits" % (value,))
        return bytes.fromhex(value)
