"""Build a pinned oracle-reachability fixture for one Aave V3 reserve.

This is the only networked step of the analysis. It walks the price path
that the protocol actually reads, records what each layer supports and
what it refuses, and writes the result to JSON. Every downstream report
and test then runs offline against that file.

A probe that no endpoint can answer aborts the build. Recording an
endpoint outage as "the contract does not support this" would put an
unverified claim into the evidence.

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
from .rpc import EthRpc

SCHEMA = "aave-oracle-reachability/1"

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
