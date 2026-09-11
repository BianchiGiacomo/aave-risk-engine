"""Build a pinned oracle-reachability fixture for one Aave V3 reserve.

This is the only networked step of the analysis. It walks the price path
that the protocol actually reads, records what each layer supports and
what it refuses, and writes the result to JSON. Every downstream report
and test then runs offline against that file.

A probe that no endpoint can answer aborts the build. Recording an
endpoint outage as "the contract does not support this" would put an
unverified claim into the evidence.

The builder also runs behavioural probes. For each scenario it replaces
the code of one feed for the duration of a single eth_call and asks the
real AaveOracle for the price, so the deployed adapter and oracle execute
unchanged against a stated input. Every mock is first called directly
under the same override on the same endpoint, and the probe aborts if the
override was not honoured. The builder records observations only; the
comparison with expected values happens offline.

Usage (from the parent directory):
  python -m aave_risk_engine.data.build_oracle_fixture \
      --chain ethereum --asset wstETH --out data/oracle/<name>.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from . import aave_v3
from .abi import (
    decode_address,
    decode_int256,
    decode_string,
    decode_words,
    encode_address,
    encode_uint,
    selector,
)
from .evm_mock import feed_bytecode, rate_bytecode
from .rpc import EthRpc

SCHEMA = "aave-oracle-reachability/2"

# Calls whose presence in a contract's bytecode would indicate that it reads
# a timestamp or a bound from the layer beneath it.
TIMESTAMP_READERS = (
    "latestRoundData()",
    "latestTimestamp()",
    "latestRound()",
    "getRoundData(uint80)",
)
BOUND_READERS = ("minAnswer()", "maxAnswer()")

STALE_AGE_SECONDS = 30 * 86_400

# Signatures probed on the Aave-side price source. The set spans the
# Chainlink-style interface plus the two Aave adapter families, so the
# fixture records which interface the source actually implements.
SOURCE_PROBES = (
    "description()",
    "decimals()",
    "DECIMALS()",
    "latestAnswer()",
    "latestRoundData()",
    "version()",
    "aggregator()",
    "minAnswer()",
    "maxAnswer()",
    "BASE_TO_USD_AGGREGATOR()",
    "RATIO_PROVIDER()",
    "ASSET_TO_PEG()",
    "PEG_TO_BASE()",
    "RATIO_DECIMALS()",
    "MINIMUM_SNAPSHOT_DELAY()",
    "getSnapshotRatio()",
    "getSnapshotTimestamp()",
    "getMaxRatioGrowthPerSecond()",
    "getMaxYearlyGrowthRatePercent()",
    "isCapped()",
)

FEED_PROBES = (
    "description()",
    "decimals()",
    "version()",
    "latestAnswer()",
    "latestRoundData()",
    "aggregator()",
    "phaseId()",
)

AGGREGATOR_PROBES = (
    "description()",
    "decimals()",
    "minAnswer()",
    "maxAnswer()",
    "latestAnswer()",
    "latestRoundData()",
    "latestTimestamp()",
    "typeAndVersion()",
)

# Exchange-rate readers an adapter might call on its ratio provider. The
# builder reports which of these appear in the adapter bytecode.
RATIO_METHOD_CANDIDATES = (
    "getPooledEthByShares(uint256)",
    "getSharesByPooledEth(uint256)",
    "stEthPerToken()",
    "tokensPerStEth()",
    "getRate()",
    "exchangeRate()",
    "getExchangeRate()",
    "convertToAssets(uint256)",
    "pricePerShare()",
)

_STRING_RETURNING = frozenset({"description()", "typeAndVersion()"})
_ADDRESS_RETURNING = frozenset(
    {
        "aggregator()",
        "BASE_TO_USD_AGGREGATOR()",
        "RATIO_PROVIDER()",
        "ASSET_TO_PEG()",
        "PEG_TO_BASE()",
    }
)
_SIGNED_RETURNING = frozenset({"latestAnswer()", "minAnswer()", "maxAnswer()"})

# ACL roles read to attribute authority over the reserve and the oracle.
ACL_ROLE_CHECKS = (
    "isPoolAdmin(address)",
    "isAssetListingAdmin(address)",
    "isRiskAdmin(address)",
    "isEmergencyAdmin(address)",
)


class FixtureError(RuntimeError):
    """A probe could not be resolved, so the fixture would be unsound."""


def _decode(signature: str, raw: str):
    if signature in _STRING_RETURNING:
        return decode_string(raw)
    words = decode_words(raw)
    if signature in _ADDRESS_RETURNING:
        return decode_address(words[0])
    if signature in _SIGNED_RETURNING:
        return decode_int256(words[0])
    if signature == "latestRoundData()":
        return {
            "round_id": words[0],
            "answer": decode_int256(words[1]),
            "started_at": words[2],
            "updated_at": words[3],
            "answered_in_round": words[4],
        }
    if signature == "isCapped()":
        return bool(words[0])
    return words[0] if len(words) == 1 else words


def probe(
    rpc: EthRpc,
    address: str,
    signatures: tuple[str, ...],
    block: int,
) -> tuple[dict, list[str]]:
    """Read each signature, separating supported values from refusals."""

    supported: dict[str, object] = {}
    reverted: list[str] = []
    for signature in signatures:
        raw, status = rpc.try_eth_call(address, selector(signature), block)
        if status == "unavailable":
            raise FixtureError(
                f"no endpoint answered {signature} on {address}; "
                "refusing to record it as unsupported"
            )
        if status == "reverted":
            reverted.append(signature)
            continue
        supported[signature] = _decode(signature, raw)
    return supported, reverted


def _call_or_fail(rpc: EthRpc, address: str, signature: str, arg: str, block: int):
    raw, status = rpc.try_eth_call(address, selector(signature) + arg, block)
    if status != "ok":
        raise FixtureError(f"{signature} on {address} returned {status}")
    return raw


def detect_ratio_method(
    rpc: EthRpc, adapter: str, ratio_provider: str, block: int
) -> dict | None:
    """Find which exchange-rate call the adapter embeds, then confirm it."""

    code = rpc.call("eth_getCode", [adapter, rpc.block_tag(block)])
    body = code.removeprefix("0x").lower()
    present = [
        signature
        for signature in RATIO_METHOD_CANDIDATES
        if selector(signature).removeprefix("0x") in body
    ]
    if len(present) != 1:
        return {
            "candidates_found": present,
            "resolved": False,
            "note": "bytecode did not identify exactly one exchange-rate call",
        }
    signature = present[0]
    arg = encode_uint(10**18) if "uint256" in signature else ""
    raw = _call_or_fail(rpc, ratio_provider, signature, arg, block)
    return {
        "candidates_found": present,
        "resolved": True,
        "method": signature,
        "selector": selector(signature),
        "argument": "1e18" if arg else None,
        "value_raw": decode_words(raw)[0],
    }


def read_controls(rpc: EthRpc, chain, data_provider: str, asset: str, block: int):
    provider = chain.addresses_provider
    acl_manager = decode_address(
        decode_words(
            _call_or_fail(rpc, provider, "getACLManager()", "", block)
        )[0]
    )
    acl_admin = decode_address(
        decode_words(
            _call_or_fail(rpc, provider, "getACLAdmin()", "", block)
        )[0]
    )
    roles = {}
    for check in ACL_ROLE_CHECKS:
        raw = _call_or_fail(
            rpc, acl_manager, check, encode_address(acl_admin), block
        )
        roles[check] = bool(decode_words(raw)[0])
    paused = bool(
        decode_words(
            _call_or_fail(
                rpc,
                data_provider,
                "getPaused(address)",
                encode_address(asset),
                block,
            )
        )[0]
    )
    return {
        "acl_manager": acl_manager,
        "acl_admin": acl_admin,
        "acl_admin_roles": roles,
        "reserve_paused": paused,
        "note": (
            "AaveOracle.setAssetSources is restricted to asset listing or "
            "pool admins; every control here requires an operator "
            "transaction and none is automatic."
        ),
    }


def embedded_calls(
    rpc: EthRpc, address: str, block: int, signatures: tuple[str, ...]
) -> dict[str, bool]:
    """Which selectors appear as constants in a contract's bytecode.

    Absence is evidence the contract does not make that call; it does not
    exclude a call whose selector is assembled at run time.
    """

    code = rpc.call("eth_getCode", [address, rpc.block_tag(block)])
    body = code.removeprefix("0x").lower()
    return {
        signature: selector(signature).removeprefix("0x") in body
        for signature in signatures
    }


def _override_endpoint(chain, feed: str, block: int) -> tuple[EthRpc, str]:
    """First endpoint that demonstrably honours eth_call state overrides."""

    sentinel = 123_456_789
    overrides = {feed: {"code": feed_bytecode(sentinel, 8, 1, 1, "fresh")}}
    for endpoint in chain.rpc_endpoints:
        candidate = EthRpc(endpoints=(endpoint,))
        raw, status = candidate.try_eth_call(
            feed, selector("latestAnswer()"), block, overrides
        )
        if status == "ok" and decode_int256(decode_words(raw)[0]) == sentinel:
            return candidate, endpoint
    raise FixtureError("no endpoint honours eth_call state overrides here")


def _oracle_price(rpc, oracle, asset, block, overrides) -> dict:
    raw, status = rpc.try_eth_call(
        oracle,
        selector("getAssetPrice(address)") + encode_address(asset),
        block,
        overrides,
    )
    if status == "unavailable":
        raise FixtureError("oracle probe went unanswered on the override endpoint")
    return {
        "status": status,
        "price_raw": decode_words(raw)[0] if status == "ok" else None,
    }


def _feed_scenario(
    rpc: EthRpc,
    name: str,
    purpose: str,
    feed: str,
    answer: int,
    updated_at: int,
    timestamps: str,
    context: dict,
) -> dict:
    overrides = {
        feed: {
            "code": feed_bytecode(
                answer, context["feed_decimals"], 1, updated_at, timestamps
            )
        }
    }
    raw, status = rpc.try_eth_call(
        feed, selector("latestAnswer()"), context["block"], overrides
    )
    if status != "ok" or decode_int256(decode_words(raw)[0]) != answer:
        raise FixtureError(f"override not honoured for scenario {name}")
    _, round_status = rpc.try_eth_call(
        feed, selector("latestRoundData()"), context["block"], overrides
    )
    expected_round = "reverted" if timestamps == "revert" else "ok"
    if round_status != expected_round:
        raise FixtureError(
            f"mock round data returned {round_status} in scenario {name}"
        )
    return {
        "name": name,
        "purpose": purpose,
        "overridden": {"layer": "base_feed", "address": feed},
        "inputs": {
            "base_answer_raw": answer,
            "updated_at": updated_at,
            "timestamp_getters": timestamps,
        },
        "mock_verified": True,
        "oracle": _oracle_price(
            rpc, context["oracle"], context["asset"], context["block"], overrides
        ),
    }


def _rate_scenario(
    rpc: EthRpc,
    name: str,
    purpose: str,
    provider: str,
    method: str,
    rate: int,
    context: dict,
) -> dict:
    overrides = {
        provider: {
            "code": rate_bytecode(method, rate, context["rate_decimals"])
        }
    }
    argument = encode_uint(10**18) if "(uint256)" in method else ""
    raw, status = rpc.try_eth_call(
        provider, selector(method) + argument, context["block"], overrides
    )
    if status != "ok" or decode_words(raw)[0] != rate:
        raise FixtureError(f"override not honoured for scenario {name}")
    return {
        "name": name,
        "purpose": purpose,
        "overridden": {"layer": "ratio_provider", "address": provider},
        "inputs": {"rate_raw": rate},
        "mock_verified": True,
        "oracle": _oracle_price(
            rpc, context["oracle"], context["asset"], context["block"], overrides
        ),
    }


def probe_behaviour(chain, fixture: dict) -> dict:
    """Run the four roadmap scenarios through the deployed contracts."""

    block = fixture["block"]
    now = fixture["block_timestamp"]
    base = fixture["base_feed"]
    feed = base["address"]
    base_answer = base["supported"]["latestAnswer()"]
    source = fixture["source"]["supported"]
    rpc, endpoint = _override_endpoint(chain, feed, block)

    context = {
        "block": block,
        "oracle": fixture["aave_oracle"]["address"],
        "asset": fixture["asset"]["address"],
        "feed_decimals": base["supported"]["decimals()"],
        "rate_decimals": source.get("RATIO_DECIMALS()", 18),
    }

    def feed_case(name, purpose, answer, updated_at, timestamps="fresh"):
        return _feed_scenario(
            rpc, name, purpose, feed, answer, updated_at, timestamps, context
        )

    scenarios = [
        {
            "name": "baseline",
            "purpose": "unmodified state at the pinned block",
            "overridden": None,
            "inputs": {},
            "mock_verified": None,
            "oracle": _oracle_price(
                rpc, context["oracle"], context["asset"], block, None
            ),
        },
        feed_case(
            "stress_50",
            "accepted stress update: base price halves",
            base_answer // 2,
            now,
        ),
        feed_case(
            "stress_99",
            "accepted stress update: base price falls 99%",
            base_answer // 100,
            now,
        ),
        feed_case(
            "stress_floor",
            "accepted stress update at the aggregator minimum of one raw unit",
            1,
            now,
        ),
        feed_case(
            "zero_answer",
            "a value the aggregator bounds would refuse to store, forced "
            "through to show what the adapter and oracle do with it",
            0,
            now,
        ),
        feed_case(
            "negative_answer",
            "a negative value, forced through for the same reason",
            -1,
            now,
        ),
        feed_case(
            "timestamps_unreadable",
            "elapsed time: every timestamp getter on the feed reverts, so "
            "any reader of a timestamp on this path would fail",
            base_answer,
            now,
            "revert",
        ),
        feed_case(
            "stale_30_days",
            "elapsed time: the round is thirty days old",
            base_answer,
            now - STALE_AGE_SECONDS,
        ),
        feed_case(
            "recovery",
            "recovery: a later valid update two percent above the baseline",
            base_answer * 102 // 100,
            now,
        ),
    ]

    ratio = fixture.get("ratio_provider") or {}
    if ratio.get("resolved") and "getSnapshotRatio()" in source:
        ceiling = source["getSnapshotRatio()"] + source[
            "getMaxRatioGrowthPerSecond()"
        ] * (now - source["getSnapshotTimestamp()"])
        for name, purpose, rate in (
            (
                "rate_above_cap",
                "exchange rate ten percent above the growth-cap ceiling",
                ceiling * 110 // 100,
            ),
            (
                "rate_drop_20",
                "exchange rate twenty percent below its current value",
                ratio["value_raw"] * 80 // 100,
            ),
        ):
            scenarios.append(
                _rate_scenario(
                    rpc,
                    name,
                    purpose,
                    ratio["address"],
                    ratio["method"],
                    rate,
                    context,
                )
            )

    return {
        "method": "eth_call with state overrides at the pinned block",
        "endpoint": endpoint,
        "scenarios": scenarios,
    }


def build(chain_name: str, asset_symbol: str, block: int | None = None) -> dict:
    chain = aave_v3.CHAINS[chain_name]
    rpc = chain.make_rpc()
    if block is None:
        # Step back from the tip so the pinned block is settled.
        block = rpc.block_number() - 64
    block_timestamp = rpc.block_timestamp(block)

    asset = chain.tokens[asset_symbol]
    data_provider, oracle = aave_v3.resolve_contracts(rpc, chain, block)

    price_raw = decode_words(
        _call_or_fail(
            rpc, oracle, "getAssetPrice(address)", encode_address(asset), block
        )
    )[0]
    fallback = decode_address(
        decode_words(
            _call_or_fail(rpc, oracle, "getFallbackOracle()", "", block)
        )[0]
    )
    source = decode_address(
        decode_words(
            _call_or_fail(
                rpc,
                oracle,
                "getSourceOfAsset(address)",
                encode_address(asset),
                block,
            )
        )[0]
    )

    source_values, source_reverted = probe(rpc, source, SOURCE_PROBES, block)

    fixture = {
        "schema": SCHEMA,
        "chain": chain_name,
        "block": block,
        "block_timestamp": block_timestamp,
        "retrieved_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "asset": {"symbol": asset_symbol, "address": asset},
        "reserve": {
            **aave_v3.reserve_configuration(rpc, data_provider, asset, block),
        },
        "controls": read_controls(rpc, chain, data_provider, asset, block),
        "aave_oracle": {
            "address": oracle,
            "data_provider": data_provider,
            "asset_price_raw": price_raw,
            "fallback_oracle": fallback,
        },
        "source": {
            "address": source,
            "supported": source_values,
            "reverted": source_reverted,
            "embedded_calls": embedded_calls(
                rpc, source, block, TIMESTAMP_READERS + BOUND_READERS
            ),
        },
    }

    for signature, key in (
        ("BASE_TO_USD_AGGREGATOR()", "base_feed"),
        ("ASSET_TO_PEG()", "asset_to_peg_feed"),
        ("PEG_TO_BASE()", "peg_to_base_feed"),
    ):
        address = source_values.get(signature)
        if not address or int(address, 16) == 0:
            continue
        values, reverted = probe(rpc, address, FEED_PROBES, block)
        entry = {"address": address, "supported": values, "reverted": reverted}
        aggregator = values.get("aggregator()")
        if aggregator and int(aggregator, 16) != 0:
            agg_values, agg_reverted = probe(
                rpc, aggregator, AGGREGATOR_PROBES, block
            )
            entry["aggregator"] = {
                "address": aggregator,
                "supported": agg_values,
                "reverted": agg_reverted,
            }
        fixture[key] = entry

    ratio_provider = source_values.get("RATIO_PROVIDER()")
    if ratio_provider and int(ratio_provider, 16) != 0:
        fixture["ratio_provider"] = {
            "address": ratio_provider,
            **(detect_ratio_method(rpc, source, ratio_provider, block) or {}),
        }

    if "base_feed" in fixture:
        fixture["behaviour"] = probe_behaviour(chain, fixture)

    return fixture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chain", default="ethereum")
    parser.add_argument("--asset", default="wstETH")
    parser.add_argument("--block", type=int, default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    fixture = build(args.chain, args.asset, args.block)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path} at block {fixture['block']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
