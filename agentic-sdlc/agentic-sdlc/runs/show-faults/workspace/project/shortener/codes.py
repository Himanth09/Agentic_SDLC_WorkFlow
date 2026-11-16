"""Short-code generation.

Random base62 codes from a CSPRNG (not sequential IDs): codes are not
enumerable, generation needs no central coordinator so every instance can
allocate independently, and with 62^7 ~ 3.5e12 codes the collision probability
stays negligible for billions of links. The rare collision is detected by the
storage layer's uniqueness constraint and retried by the service.
"""
from __future__ import annotations

# secrets: cryptographically secure randomness (codes must not be guessable).
import secrets
# string: the digit and letter sets that make up the alphabet.
import string
# Callable: type of the injectable random function.
from typing import Callable

# 62 URL-safe characters: 0-9, a-z, A-Z.
ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase
# Number base (62).
BASE = len(ALPHABET)


# Integer -> base62 string, left-padded with "0" to `width` characters.
def encode_base62(n: int, width: int = 0) -> str:
    if n < 0:
        raise ValueError("n must be non-negative")
    out = []
    # Repeatedly take the lowest base62 digit (digits come out in reverse order).
    while n:
        n, rem = divmod(n, BASE)
        out.append(ALPHABET[rem])
    # Reverse, pad to the requested width; zero encodes as a single "0".
    return "".join(reversed(out)).rjust(width, ALPHABET[0]) or ALPHABET[0]


# Base62 string -> integer (the inverse of encode_base62).
def decode_base62(s: str) -> int:
    n = 0
    for ch in s:
        idx = ALPHABET.find(ch)
        # Reject anything outside the alphabet.
        if idx < 0:
            raise ValueError(f"invalid base62 character {ch!r}")
        n = n * BASE + idx
    return n


# Produces random fixed-length codes; uniqueness is enforced later by the database.
class CodeGenerator:
    # `randbelow` is injectable so tests can force collisions deterministically.
    def __init__(self, length: int, randbelow: Callable[[int], int] = secrets.randbelow) -> None:
        # Number of characters in every generated code.
        self.length = length
        # Size of the code space: 62 ** length possible codes.
        self.space = BASE ** length
        self._randbelow = randbelow

    # Pick a uniformly random number in the code space and encode it at full width.
    def next(self) -> str:
        return encode_base62(self._randbelow(self.space), self.length)
