"""Which encoder a destination gets when it names none (docs/goat-transformers-encoder.md):
the library for CoT and ASTERIX CAT062, the native encoder for SAPIENT, and the native
encoder for a CoT destination that sets a key only the native encoder writes."""

from __future__ import annotations

from open_entity_gateway.library import choose


def test_cot_and_cat062_default_to_the_library() -> None:
    assert choose({}, "t", native_only=("time_form",), default="goat-transformers") == "goat-transformers"
    assert choose({}, "a", default="goat-transformers") == "goat-transformers"


def test_sapient_and_an_unnamed_default_stay_native() -> None:
    assert choose({}, "s") == "native"


def test_a_native_only_key_keeps_the_native_encoder_when_none_is_named() -> None:
    destination = {"time_form": "first-bite"}
    assert choose(destination, "t", native_only=("time_form",), default="goat-transformers") == "native"


def test_a_named_encoder_wins_over_the_default() -> None:
    assert choose({"encoder": "native"}, "t", default="goat-transformers") == "native"
    assert choose({"encoder": "goat-transformers"}, "s") == "goat-transformers"
