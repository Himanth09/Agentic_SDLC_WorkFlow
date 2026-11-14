"""Short-code generation.

Random base62 codes from a CSPRNG (not sequential IDs): codes are not
enumerable, generation needs no central coordinator so every instance can
allocate independently, and with 62^7 ~ 3.5e12 codes the collision probability
stays negligible for billions of links. The rare collision is detected by the
storage layer's uniqueness constraint and retried by the service.
"""
from __future__ import annotations

import secrets
import string
from typing import Callable

ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase
BASE = len(ALPHABET)


def encode_base62(n: int, width: int = 0) -> str:
    if n < 0:
        raise ValueError("n must be non-negative")
    out = []
    while n:
        n, rem = divmod(n, BASE)
        out.append(ALPHABET[rem])
    return "".join(reversed(out)).rjust(width, ALPHABET[0]) or ALPHABET[0]


def decode_base62(s: str) -> int:
    n = 0
    for ch in s:
        idx = ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"invalid base62 character {ch!r}")
        n = n * BASE + idx
    return n


class CodeGenerator:
    def __init__(self, length: int, randbelow: Callable[[int], int] = secrets.randbelow) -> None:
        self.length = length
        self.space = BASE ** length
        self._randbelow = randbelow

    def next(self) -> str:
        return encode_base62(self._randbelow(self.space), self.length)
