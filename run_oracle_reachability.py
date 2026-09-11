"""Answer test 1 for one Aave reserve: can a stress price reach the pool?"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import sys
import textwrap
from pathlib import Path

from .oracle_reachability import assess, load_fixture

_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_FIXTURE = (
    _PACKAGE_DIR / "data" / "oracle" / "ethereum-wsteth-25946216.json"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: str, payload: dict) -> str:
    absolute = os.path.abspath(path)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with open(absolute, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return absolute


def _price(raw: int | None, decimals: int) -> str:
    return "-" if raw is None else f"{raw / 10**decimals:,.8f}"


def _wrap(text: str, indent: str = "  ") -> str:
    return textwrap.fill(
        text, width=78, initial_indent=indent, subsequent_indent=indent + "  "
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Oracle reachability for an Aave V3 reserve"
    )
    parser.add_argument("--fixture", default=str(_DEFAULT_FIXTURE))
    parser.add_argument("--manifest")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    fixture_path = Path(args.fixture).resolve()
    fixture = load_fixture(fixture_path)
    result = assess(fixture)
    source = fixture["source"]
    decimals = source["supported"]["decimals()"]

    print("Oracle reachability (test 1 of the four-test sequence)")
    print(
        f"  reserve    : {result.asset} on {result.chain} | block "
        f"{result.block:,} | {fixture['block_timestamp']}"
    )
    print(
        "  fixture    : "
        + os.path.relpath(fixture_path, _PACKAGE_DIR).replace("\\", "/")
    )

    print("\nPrice path the protocol reads")
    print(f"  AaveOracle      {fixture['aave_oracle']['address']}")
    print(
        f"  price source    {source['address']} "
        f"{source['supported']['description()']!r}"
    )
    base = fixture["base_feed"]
    print(
        f"  base feed       {base['address']} "
        f"{base['supported'].get('description()', '')!r}"
    )
    if base.get("aggregator"):
        print(
            f"  aggregator      {base['aggregator']['address']} "
            f"{base['aggregator']['supported'].get('typeAndVersion()', '')!r}"
        )
    ratio = fixture.get("ratio_provider") or {}
    if ratio.get("resolved"):
        print(f"  ratio provider  {ratio['address']} via {ratio['method']}")

    rec = result.reconstruction
    if rec is not None:
        print("\nBaseline reconstruction")
        print(f"  reconstructed    {_price(rec.reconstructed_raw, decimals)}")
        print(f"  source answer    {_price(rec.source_raw, decimals)}")
        print(f"  AaveOracle price {_price(rec.oracle_raw, decimals)}")
        print(
            f"  matches source {rec.matches_source} | matches oracle "
            f"{rec.matches_oracle}"
        )

    if result.scenarios:
        endpoint = fixture["behaviour"]["endpoint"]
        print(
            "\nBehaviour of the deployed contracts "
            f"(eth_call state overrides via {endpoint})"
        )
        print(
            "  scenario               status    observed          "
            "expected          match"
        )
        for check in result.scenarios:
            match = "-" if check.matches is None else str(check.matches)
            print(
                f"  {check.name:<22} {check.observed_status:<9} "
                f"{_price(check.observed_raw, decimals):>17} "
                f"{_price(check.expected_raw, decimals):>17} {match}"
            )

    fresh = result.freshness
    if fresh is not None:
        print("\nFreshness, as four separate properties")
        print(f"  source exposes a timestamp          {fresh.source_exposes_timestamp}")
        print(
            "  source bytecode embeds a reader     "
            f"{fresh.source_embeds_timestamp_call}"
        )
        print(
            "  timestamp read on executed path     "
            f"{fresh.timestamp_read_on_executed_path}"
        )
        print(f"  staleness threshold enforced        {fresh.enforced}")
        if fresh.feed_age_seconds is not None:
            print(
                "  feed round age at the pinned block  "
                f"{fresh.feed_age_seconds:,} s"
            )

    print("\nBounds exposed by the path")
    for bound in result.bounds:
        print(f"  {bound.kind} at {bound.layer}: {bound.raw_value}")
    if result.max_verified_fall is not None:
        print(f"  verified representable fall: {result.max_verified_fall:.6%}")

    print(f"\nVerdict on test 1: {result.verdict}")
    for reason in result.reasons:
        print(_wrap(f"reason: {reason}"))
    for caveat in result.caveats:
        print(_wrap(f"caveat: {caveat}"))

    if args.manifest:
        payload = {
            "schema_version": 2,
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "command": [
                "python",
                "-m",
                "aave_risk_engine.run_oracle_reachability",
                *(argument.replace("\\", "/") for argument in sys.argv[1:]),
            ],
            "fixture": {
                "file": os.path.relpath(fixture_path, _PACKAGE_DIR).replace(
                    "\\", "/"
                ),
                "sha256": _sha256(fixture_path),
                "schema": fixture["schema"],
                "chain": fixture["chain"],
                "block": fixture["block"],
                "block_timestamp": fixture["block_timestamp"],
                "retrieved_at_utc": fixture["retrieved_at_utc"],
                "asset": fixture["asset"],
                "behaviour_endpoint": (fixture.get("behaviour") or {}).get(
                    "endpoint"
                ),
            },
            "definitions": {
                "test_1": (
                    "can the oracle represent the stress and transmit it to "
                    "the protocol"
                ),
                "behavioural_probe": (
                    "eth_call at the pinned block with the code of one feed "
                    "replaced by a mock serving a stated input; the mock is "
                    "verified under the same override before the real "
                    "AaveOracle is asked for the price"
                ),
                "expected_price": (
                    "base answer * min(rate, growth-cap ceiling) / 10**rate "
                    "decimals; no prediction for non-positive inputs"
                ),
                "freshness": (
                    "exposing a timestamp, embedding a reader, reading one on "
                    "the executed path, and enforcing a threshold are "
                    "recorded as separate properties"
                ),
            },
            "results": {
                "verdict": result.verdict,
                "reasons": list(result.reasons),
                "caveats": list(result.caveats),
                "reconstruction": (
                    {
                        **dataclasses.asdict(rec),
                        "matches_source": rec.matches_source,
                        "matches_oracle": rec.matches_oracle,
                        "error_raw": rec.error_raw,
                    }
                    if rec is not None
                    else None
                ),
                "scenarios": [
                    {**dataclasses.asdict(check), "matches": check.matches}
                    for check in result.scenarios
                ],
                "freshness": (
                    {**dataclasses.asdict(fresh), "enforced": fresh.enforced}
                    if fresh is not None
                    else None
                ),
                "bounds": [dataclasses.asdict(b) for b in result.bounds],
                "max_verified_fall": result.max_verified_fall,
                "refused_below_raw": result.refused_below_raw,
                "cap": (
                    {
                        **dataclasses.asdict(result.cap),
                        "headroom": result.cap.headroom,
                        "yearly_rate_consistent": result.cap.yearly_rate_consistent,
                        "cap_flag_consistent": result.cap.cap_flag_consistent,
                    }
                    if result.cap is not None
                    else None
                ),
            },
            "not_established": [
                "behaviour of any other reserve or price source",
                "behaviour at any other block, or after governance replaces "
                "the source",
                "the aggregator's own transmission path, which requires "
                "signed reports and was not executed",
                "whether an operator would pause or freeze in time",
                "the freshness of the exchange-rate provider's own inputs",
            ],
        }
        print(f"\nwrote {_write_json(args.manifest, payload)}")


if __name__ == "__main__":
    main()
