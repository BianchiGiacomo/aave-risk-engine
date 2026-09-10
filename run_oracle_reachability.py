"""Answer test 1 for one Aave reserve: can a stress price reach the pool?"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

from .oracle_reachability import (
    assess,
    load_fixture,
)

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


def _price(raw: int, decimals: int) -> str:
    return f"{raw / 10**decimals:,.8f}"


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
        f"  reserve                    : {result.asset} on {result.chain} | "
        f"block {result.block:,} | {fixture['block_timestamp']}"
    )
    print(
        f"  fixture                    : "
        f"{os.path.relpath(fixture_path, _PACKAGE_DIR)}".replace("\\", "/")
    )

    print("\nPrice path actually read by the protocol")
    print(f"  AaveOracle                 : {fixture['aave_oracle']['address']}")
    print(
        f"  price source               : {source['address']} | "
        f"{source['supported']['description()']!r}"
    )
    for key, label in (
        ("base_feed", "base feed"),
        ("asset_to_peg_feed", "asset-to-peg feed"),
        ("peg_to_base_feed", "peg-to-base feed"),
    ):
        entry = fixture.get(key)
        if not entry:
            continue
        print(
            f"  {label:<27}: {entry['address']} | "
            f"{entry['supported'].get('description()', '')!r}"
        )
        aggregator = entry.get("aggregator")
        if aggregator:
            print(
                f"  {label + ' aggregator':<27}: {aggregator['address']} | "
                f"{aggregator['supported'].get('typeAndVersion()', '')!r}"
            )
    ratio = fixture.get("ratio_provider")
    if ratio and ratio.get("resolved"):
        print(
            f"  {'ratio provider':<27}: {ratio['address']} | "
            f"{ratio['method']}"
        )

    reconstruction = result.reconstruction
    if reconstruction is not None:
        print("\nPath reconstruction (verification, not calibration)")
        print(
            f"  base feed answer           : "
            f"{_price(reconstruction.base_answer_raw, decimals)}"
        )
        print(
            f"  exchange rate              : "
            f"{reconstruction.ratio_raw / 10**reconstruction.ratio_decimals:.12f}"
        )
        print(
            f"  cap applied to rate        : {reconstruction.cap_applied}"
        )
        print(
            f"  reconstructed answer       : "
            f"{_price(reconstruction.reconstructed_raw, decimals)}"
        )
        print(
            f"  source latestAnswer        : "
            f"{_price(reconstruction.reported_raw, decimals)}"
        )
        print(
            f"  AaveOracle getAssetPrice   : "
            f"{_price(reconstruction.oracle_raw, decimals)}"
        )
        print(
            f"  exact match                : {reconstruction.matches_source} "
            f"(error {reconstruction.error_raw} raw units)"
        )

    cap = result.cap
    if cap is not None:
        print("\nExchange-rate growth cap")
        print(
            f"  snapshot rate              : "
            f"{cap.snapshot_ratio_raw / 10**18:.12f} at {cap.snapshot_timestamp}"
        )
        print(
            f"  elapsed                    : {cap.elapsed_seconds:,} s "
            f"({cap.elapsed_seconds / 86400:.1f} days)"
        )
        print(
            f"  max yearly growth          : "
            f"{cap.reported_yearly_percent:.2f}% reported | "
            f"{cap.derived_yearly_percent:.2f}% derived | "
            f"consistent {cap.yearly_rate_consistent}"
        )
        print(
            f"  ceiling on rate now        : "
            f"{cap.ceiling_ratio_raw / 10**18:.12f}"
        )
        print(
            f"  current rate               : "
            f"{cap.current_ratio_raw / 10**18:.12f} | headroom "
            f"{cap.headroom:.4%}"
        )
        print(
            f"  isCapped                   : {cap.reported_is_capped} reported "
            f"| {cap.derived_is_capped} derived | consistent "
            f"{cap.cap_flag_consistent}"
        )

    span = result.representable
    print("\nBounds on the path")
    if not span.bounds:
        print("  none found on any layer")
    for bound in span.bounds:
        print(
            f"  {bound.kind:<3} at {bound.layer:<24} {bound.address} "
            f"= {bound.raw_value}"
        )
    print(
        f"  deepest floor in price units: "
        f"{_price(span.floor_price_raw, decimals)}"
    )
    print(
        f"  representable fall from here: "
        f"{span.max_representable_drawdown:.6%}"
    )

    stale = result.staleness
    print("\nStaleness observability")
    print(
        f"  protocol-facing timestamp  : "
        f"{stale.protocol_facing_exposes_timestamp}"
    )
    print(
        f"  refused by the source      : "
        f"{', '.join(stale.protocol_facing_reverted) or 'nothing'}"
    )
    if stale.deepest_timestamp is not None:
        print(
            f"  deepest timestamp          : {stale.deepest_timestamp} at "
            f"{stale.deepest_timestamp_layer} | age {stale.age_seconds:,} s"
        )

    print(f"\nVerdict on test 1: {result.verdict}")
    for reason in result.reasons:
        print(f"  reason : {reason}")
    for caveat in result.caveats:
        print(f"  caveat : {caveat}")
    print(
        "\n  scope  : this is one integration at one block. Nothing here "
        "transfers to another asset without its own fixture."
    )

    if args.manifest:
        payload = {
            "schema_version": 1,
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
            },
            "definitions": {
                "test_1": (
                    "can the oracle represent the stress and transmit it to "
                    "the protocol"
                ),
                "reconstruction": (
                    "base feed answer * min(rate, cap ceiling) / 10**rate "
                    "decimals, compared to the on-chain source answer"
                ),
                "representable_fall": (
                    "1 - deepest floor bound translated into price units, "
                    "divided by the current price"
                ),
                "staleness_observability": (
                    "whether the layer the protocol reads exposes a "
                    "timestamp that a freshness check could inspect"
                ),
            },
            "results": {
                "verdict": result.verdict,
                "reasons": list(result.reasons),
                "caveats": list(result.caveats),
                "reconstruction": (
                    {
                        "base_answer_raw": reconstruction.base_answer_raw,
                        "ratio_raw": reconstruction.ratio_raw,
                        "capped_ratio_raw": reconstruction.capped_ratio_raw,
                        "cap_applied": reconstruction.cap_applied,
                        "reconstructed_raw": reconstruction.reconstructed_raw,
                        "reported_raw": reconstruction.reported_raw,
                        "oracle_raw": reconstruction.oracle_raw,
                        "error_raw": reconstruction.error_raw,
                        "matches_source": reconstruction.matches_source,
                        "matches_oracle": reconstruction.matches_oracle,
                    }
                    if reconstruction is not None
                    else None
                ),
                "cap": (
                    {
                        "snapshot_ratio_raw": cap.snapshot_ratio_raw,
                        "snapshot_timestamp": cap.snapshot_timestamp,
                        "max_growth_per_second": cap.max_growth_per_second,
                        "minimum_snapshot_delay_s": cap.minimum_snapshot_delay_s,
                        "elapsed_seconds": cap.elapsed_seconds,
                        "reported_yearly_percent": cap.reported_yearly_percent,
                        "derived_yearly_percent": cap.derived_yearly_percent,
                        "yearly_rate_consistent": cap.yearly_rate_consistent,
                        "ceiling_ratio_raw": cap.ceiling_ratio_raw,
                        "current_ratio_raw": cap.current_ratio_raw,
                        "headroom": cap.headroom,
                        "reported_is_capped": cap.reported_is_capped,
                        "derived_is_capped": cap.derived_is_capped,
                        "cap_flag_consistent": cap.cap_flag_consistent,
                    }
                    if cap is not None
                    else None
                ),
                "bounds": [
                    {
                        "layer": bound.layer,
                        "address": bound.address,
                        "kind": bound.kind,
                        "raw_value": bound.raw_value,
                    }
                    for bound in span.bounds
                ],
                "floor_price_raw": span.floor_price_raw,
                "max_representable_drawdown": span.max_representable_drawdown,
                "staleness": {
                    "protocol_facing_layer": stale.protocol_facing_layer,
                    "protocol_facing_exposes_timestamp": (
                        stale.protocol_facing_exposes_timestamp
                    ),
                    "protocol_facing_reverted": list(
                        stale.protocol_facing_reverted
                    ),
                    "deepest_timestamp": stale.deepest_timestamp,
                    "deepest_timestamp_layer": stale.deepest_timestamp_layer,
                    "age_seconds": stale.age_seconds,
                    "enforced_anywhere_on_path": stale.enforced_anywhere_on_path,
                },
            },
            "not_established": [
                "behaviour of any other Aave reserve or price source",
                "behaviour at any block other than the pinned one",
                "whether an operator would in fact pause or freeze in time",
                "off-chain publication rules of the underlying feed",
            ],
            "notes": [
                "A rejected update need not imply a failed read.",
                "An aging round need not imply enforced rejection.",
                "Heartbeat settings alone do not establish a freshness check.",
                "Pauses and freezes require an operator transaction here.",
                "The growth cap constrains the exchange rate, not the price.",
            ],
        }
        print(f"\nwrote {_write_json(args.manifest, payload)}")


if __name__ == "__main__":
    main()
