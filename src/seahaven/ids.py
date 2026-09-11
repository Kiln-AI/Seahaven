"""The one source of randomness inside an instance.

An instance is meant to replay: the same fixture and the same caller seed give
the same identifiers, so a test can assert on an id and two runs can be compared
changeset to changeset. That rules out `uuid.uuid4`, which reads the OS entropy
pool. World code draws from `ctx.ids` instead.
"""

import hashlib
import random
import uuid

from seahaven.errors import WorldBug

__all__ = ["Ids", "instance_seed"]

# What `seed=None` means: a named constant rather than a bare literal, because
# the default seed is part of the reproducibility contract.
DEFAULT_CALLER_SEED = b"default"


def instance_seed(source: str, caller_seed: bytes | int | None = None) -> bytes:
    """Derive an instance's seed from what it came from and what the caller asked for.

    `source` is the fixture id, or the world name for a blank instance, so the
    same caller seed against two fixtures gives two streams.
    """
    return hashlib.sha256(source.encode("utf-8") + b"\0" + _seed_bytes(caller_seed)).digest()


def _seed_bytes(caller_seed: bytes | int | None) -> bytes:
    match caller_seed:
        case None:
            return DEFAULT_CALLER_SEED
        case bytes():
            return caller_seed
        case int():
            if caller_seed < 0:
                raise WorldBug(f"a seed must not be negative: {caller_seed}")
            try:
                return caller_seed.to_bytes(8, "big")
            except OverflowError as error:
                raise WorldBug(f"a seed must fit in 8 bytes: {caller_seed}") from error
        case _:
            raise WorldBug(
                f"a seed must be bytes, an int or None, not {type(caller_seed).__name__}"
            )


class Ids:
    """Seeded identifiers and randomness, world-agnostic.

    Product-shaped keys (`ENG-13`, a sequential invoice number) are the world's
    own business, built on `random` or on its tables; this is the stream they
    draw from.
    """

    def __init__(self, seed: bytes) -> None:
        self.random = random.Random(int.from_bytes(seed))

    def uuid(self) -> str:
        """A UUIDv4-shaped identifier drawn from the seeded stream."""
        return str(uuid.UUID(int=self.random.getrandbits(128), version=4))
