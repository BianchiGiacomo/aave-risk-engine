"""Minimal EVM bytecode for mock feeds used in eth_call state overrides.

The oracle-reachability builder replaces the code at a feed address for
the duration of a single eth_call, then asks the real AaveOracle for a
price. The deployed adapter and oracle bytecode execute unchanged against
the mock, so each observation is the contracts' own behaviour under a
stated input rather than a model of it. Nothing is deployed and no state
persists beyond the call.

The builder checks every mock against a live node before relying on it:
the mock is called directly under the same override, and its answer must
equal the scenario input, otherwise the probe aborts.
"""

from __future__ import annotations

from .abi import selector

_OPCODES = {
    "MUL": 0x02,
    "DIV": 0x04,
    "EQ": 0x14,
    "SHR": 0x1C,
    "CALLDATALOAD": 0x35,
    "MSTORE": 0x52,
    "JUMPI": 0x57,
    "JUMPDEST": 0x5B,
    "DUP1": 0x80,
    "RETURN": 0xF3,
    "REVERT": 0xFD,
}

_WORD = 1 << 256


def _push(width: int, value: int) -> tuple:
    return ("PUSH", width, value % _WORD)


def assemble(program: list) -> bytes:
    """Two-pass assembler for a tiny subset of EVM.

    Items are opcode names, ("PUSH", width, value), ("LABEL", name), or
    ("JUMP_TO", name). A label emits JUMPDEST; JUMP_TO pushes its offset.
    """

    offsets: dict[str, int] = {}
    position = 0
    for item in program:
        if isinstance(item, str):
            position += 1
        elif item[0] == "PUSH":
            position += 1 + item[1]
        elif item[0] == "LABEL":
            offsets[item[1]] = position
            position += 1
        elif item[0] == "JUMP_TO":
            position += 3
        else:
            raise ValueError(f"unknown item {item!r}")

    out = bytearray()
    for item in program:
        if isinstance(item, str):
            out.append(_OPCODES[item])
        elif item[0] == "PUSH":
            width, value = item[1], item[2]
            if not 1 <= width <= 32 or value >= 1 << (8 * width):
                raise ValueError(f"value does not fit PUSH{width}")
            out.append(0x5F + width)
            out += value.to_bytes(width, "big")
        elif item[0] == "LABEL":
            out.append(_OPCODES["JUMPDEST"])
        else:
            if item[1] not in offsets:
                raise ValueError(f"undefined label {item[1]!r}")
            out.append(0x61)
            out += offsets[item[1]].to_bytes(2, "big")
    return bytes(out)


def _dispatch(routes: list[tuple[str, str]]) -> list:
    program: list = [_push(1, 0), "CALLDATALOAD", _push(1, 0xE0), "SHR"]
    for signature, label in routes:
        program += [
            "DUP1",
            _push(4, int(selector(signature), 16)),
            "EQ",
            ("JUMP_TO", label),
            "JUMPI",
        ]
    program += [_push(1, 0), "DUP1", "REVERT"]
    return program


def _return_word(value: int) -> list:
    return [_push(32, value), _push(1, 0), "MSTORE", _push(1, 32), _push(1, 0), "RETURN"]


def _revert() -> list:
    return [_push(1, 0), "DUP1", "REVERT"]


def feed_bytecode(
    answer: int,
    decimals: int,
    round_id: int,
    updated_at: int,
    timestamps: str = "fresh",
) -> str:
    """A Chainlink-style feed returning a fixed answer.

    timestamps="fresh" serves latestRoundData and latestTimestamp with the
    given updated_at; timestamps="revert" makes both revert, so any caller
    that reads a timestamp on this path fails visibly.
    """

    if timestamps not in ("fresh", "revert"):
        raise ValueError("timestamps must be 'fresh' or 'revert'")
    program = _dispatch(
        [
            ("latestAnswer()", "answer"),
            ("decimals()", "decimals"),
            ("latestRoundData()", "round"),
            ("latestTimestamp()", "timestamp"),
        ]
    )
    program += [("LABEL", "answer")] + _return_word(answer)
    program += [("LABEL", "decimals")] + _return_word(decimals)
    program += [("LABEL", "round")]
    if timestamps == "revert":
        program += _revert()
    else:
        for slot, value in enumerate(
            (round_id, answer, updated_at, updated_at, round_id)
        ):
            program += [_push(32, value), _push(1, 32 * slot), "MSTORE"]
        program += [_push(1, 0xA0), _push(1, 0), "RETURN"]
    program += [("LABEL", "timestamp")]
    program += _revert() if timestamps == "revert" else _return_word(updated_at)
    return "0x" + assemble(program).hex()


def rate_bytecode(method: str, rate: int, rate_decimals: int) -> str:
    """An exchange-rate provider answering the adapter's resolved method.

    For a one-argument method the answer scales with the argument, as
    getPooledEthByShares does; otherwise the rate is returned directly.
    """

    program = _dispatch([(method, "rate"), ("decimals()", "decimals")])
    program += [("LABEL", "rate")]
    if "(uint256)" in method:
        program += [
            _push(32, 10**rate_decimals),
            _push(1, 4),
            "CALLDATALOAD",
            _push(32, rate),
            "MUL",
            "DIV",
            _push(1, 0),
            "MSTORE",
            _push(1, 32),
            _push(1, 0),
            "RETURN",
        ]
    else:
        program += _return_word(rate)
    program += [("LABEL", "decimals")] + _return_word(rate_decimals)
    return "0x" + assemble(program).hex()
