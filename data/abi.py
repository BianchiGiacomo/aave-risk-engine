"""Minimal ABI helpers: Keccak-256, function selectors, word decoding.

Selectors are derived from their signatures rather than hardcoded, so a
typo produces a revert against a known signature instead of a silent call
to the wrong function.

hashlib.sha3_256 cannot be used here: Ethereum uses original Keccak
padding (0x01), while NIST SHA3 uses 0x06, so the digests differ.
"""

from __future__ import annotations

_MASK64 = 0xFFFFFFFFFFFFFFFF
_RATE_BYTES = 136  # 1088-bit rate for Keccak-256

_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

# Rotation offsets indexed [x][y].
_ROTATIONS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


def _rotl64(value: int, shift: int) -> int:
    if shift == 0:
        return value
    return ((value << shift) | (value >> (64 - shift))) & _MASK64


def _permute(lanes: list[list[int]]) -> None:
    for rnd in range(24):
        column = [
            lanes[x][0] ^ lanes[x][1] ^ lanes[x][2] ^ lanes[x][3] ^ lanes[x][4]
            for x in range(5)
        ]
        delta = [
            column[(x - 1) % 5] ^ _rotl64(column[(x + 1) % 5], 1) for x in range(5)
        ]
        for x in range(5):
            for y in range(5):
                lanes[x][y] ^= delta[x]

        moved = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                moved[y][(2 * x + 3 * y) % 5] = _rotl64(
                    lanes[x][y], _ROTATIONS[x][y]
                )

        for x in range(5):
            for y in range(5):
                lanes[x][y] = moved[x][y] ^ (
                    (moved[(x + 1) % 5][y] ^ _MASK64) & moved[(x + 2) % 5][y]
                )

        lanes[0][0] ^= _ROUND_CONSTANTS[rnd]


def keccak256(data: bytes) -> bytes:
    """Original Keccak-256, as used for Ethereum ABI selectors."""

    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % _RATE_BYTES != 0:
        padded.append(0x00)
    padded[-1] ^= 0x80

    lanes = [[0] * 5 for _ in range(5)]
    for offset in range(0, len(padded), _RATE_BYTES):
        block = padded[offset : offset + _RATE_BYTES]
        for i in range(_RATE_BYTES // 8):
            lanes[i % 5][i // 5] ^= int.from_bytes(
                block[i * 8 : i * 8 + 8], "little"
            )
        _permute(lanes)

    out = bytearray()
    for i in range(4):
        out += lanes[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out)


def selector(signature: str) -> str:
    """First four bytes of keccak256(signature), as a 0x-prefixed string."""

    if "(" not in signature or not signature.endswith(")"):
        raise ValueError(f"not a function signature: {signature!r}")
    return "0x" + keccak256(signature.encode()).hex()[:8]


def encode_address(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def encode_uint(value: int) -> str:
    if value < 0:
        raise ValueError("uint argument must be non-negative")
    return f"{value:064x}"


def decode_words(hex_result: str) -> list[int]:
    raw = hex_result.removeprefix("0x")
    if len(raw) % 64 != 0:
        raise ValueError("ABI result is not a whole number of 32-byte words")
    return [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]


def decode_address(word: int) -> str:
    return "0x" + f"{word:040x}"


def decode_int256(word: int) -> int:
    """Interpret a 32-byte word as a signed two's-complement integer."""

    return word - (1 << 256) if word >= (1 << 255) else word


def decode_string(hex_result: str) -> str:
    """Decode an ABI-encoded dynamic string."""

    raw = hex_result.removeprefix("0x")
    if len(raw) < 128:
        raise ValueError("result too short to hold an ABI string")
    length = int(raw[64:128], 16)
    body = raw[128 : 128 + length * 2]
    if len(body) < length * 2:
        raise ValueError("ABI string body is truncated")
    return bytes.fromhex(body).decode("utf-8")
