"""Seeded identifiers: the same inputs replay to the same stream."""

import uuid

import pytest

from seahaven.errors import WorldBug
from seahaven.ids import Ids, instance_seed


def uuids(source: str, caller_seed: bytes | int | None = None, count: int = 5) -> list[str]:
    ids = Ids(instance_seed(source, caller_seed))
    return [ids.uuid() for _ in range(count)]


def test_the_same_seed_replays_the_same_stream() -> None:
    assert uuids("agency") == uuids("agency")

    first, second = Ids(instance_seed("agency")), Ids(instance_seed("agency"))
    assert [first.random.random() for _ in range(3)] == [second.random.random() for _ in range(3)]


def test_different_caller_seeds_diverge() -> None:
    assert uuids("agency", 1) != uuids("agency", 2)
    assert uuids("agency", b"one") != uuids("agency", b"two")
    assert uuids("agency", None) != uuids("agency", 0)


def test_the_source_is_mixed_in() -> None:
    assert uuids("agency", 7) != uuids("startup", 7)


def test_caller_seed_forms() -> None:
    assert instance_seed("agency") == instance_seed("agency", b"default")
    assert instance_seed("agency", 1) == instance_seed("agency", (1).to_bytes(8, "big"))
    assert len(instance_seed("agency")) == 32


def test_a_negative_seed_is_refused() -> None:
    with pytest.raises(WorldBug, match="must not be negative"):
        instance_seed("agency", -1)


def test_a_seed_wider_than_eight_bytes_is_refused() -> None:
    with pytest.raises(WorldBug, match="8 bytes"):
        instance_seed("agency", 2**64)


def test_a_seed_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(WorldBug, match="bytes, an int or None"):
        instance_seed("agency", "seed")  # ty: ignore[invalid-argument-type]


def test_uuids_are_version_four_shaped() -> None:
    for text in uuids("agency"):
        parsed = uuid.UUID(text)
        assert parsed.version == 4
        assert parsed.variant == uuid.RFC_4122
        assert str(parsed) == text
