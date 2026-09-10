"""Render the four-test assessment for one reserve from pinned manifests."""

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

from .four_test_assessment import build_assessment, load_manifest

_PACKAGE_DIR = Path(__file__).resolve().parent
_MANIFESTS = _PACKAGE_DIR / "docs" / "manifests"

_DEFAULT_STRESS = (
    "two-day horizon, Student-t returns with jumps, correlated peg and "
    "depth stress, as pinned in the market report manifest"
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


def _wrap(text: str, indent: str = "    ") -> str:
    return textwrap.fill(
        text, width=78, initial_indent=indent, subsequent_indent=indent
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Four-test assessment for one Aave reserve"
    )
    parser.add_argument(
        "--reachability",
        default=str(_MANIFESTS / "ethereum-wsteth-oracle-reachability-25780402.json"),
    )
    parser.add_argument(
        "--market", default=str(_MANIFESTS / "ethereum-wsteth-2026-08-18.json")
    )
    parser.add_argument(
        "--balance-sheet",
        default=str(
            _MANIFESTS / "ethereum-wsteth-liquidator-balance-sheet-2026-08-18.json"
        ),
    )
    parser.add_argument("--canonical-loss", type=float, default=0.04)
    parser.add_argument("--stress-path", default=_DEFAULT_STRESS)
    parser.add_argument("--manifest")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    paths = {
        "reachability": Path(args.reachability).resolve(),
        "market": Path(args.market).resolve(),
        "balance_sheet": Path(args.balance_sheet).resolve(),
    }
    loaded = {
        key: (
            os.path.relpath(path, _PACKAGE_DIR).replace("\\", "/"),
            load_manifest(path),
        )
        for key, path in paths.items()
    }

    result = build_assessment(
        loaded["reachability"],
        loaded["market"],
        loaded["balance_sheet"],
        stress_path=args.stress_path,
        canonical_loss=args.canonical_loss,
    )

    print("Four-test assessment")
    print(
        f"  reserve      : {result.asset} on {result.chain} | block "
        f"{result.block:,} | {result.block_timestamp}"
    )
    print(f"  stress path  : {result.stress_path}")
    print(
        f"  one vintage  : {result.vintage_consistent}"
        + ("" if result.vintage_consistent else "  <-- manifests disagree")
    )

    print("\n test | verdict       | question")
    print("------|---------------|" + "-" * 56)
    for test in result.tests:
        print(f"  {test.number}   | {test.verdict:<13} | {test.question}")

    for test in result.tests:
        print(f"\nTest {test.number}: {test.verdict}")
        print(_wrap(f"criterion: {test.criterion}"))
        print(_wrap(f"finding: {test.finding}"))
        for item in test.evidence:
            print(f"    evidence: {item.manifest} :: {item.field}")
        for item in test.missing:
            print(_wrap(f"missing: {item}"))
        if test.who_can_supply:
            print(_wrap(f"who can supply: {', '.join(test.who_can_supply)}"))

    print(f"\nOverall clearance: {result.overall}")
    print(_wrap(result.overall_reason, indent="  "))
    print(
        _wrap(
            "Scope: one reserve, one block, one stress path. Nothing here "
            "transfers to another asset or another date without its own "
            "evidence.",
            indent="  ",
        )
    )

    if args.manifest:
        payload = {
            "schema_version": 1,
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "command": [
                "python",
                "-m",
                "aave_risk_engine.run_four_test_assessment",
                *(argument.replace("\\", "/") for argument in sys.argv[1:]),
            ],
            "inputs": {
                key: {
                    "file": loaded[key][0],
                    "sha256": _sha256(path),
                }
                for key, path in paths.items()
            },
            "scope": {
                "chain": result.chain,
                "asset": result.asset,
                "block": result.block,
                "block_timestamp": result.block_timestamp,
                "stress_path": result.stress_path,
                "canonical_loss": args.canonical_loss,
                "vintage_consistent": result.vintage_consistent,
            },
            "tests": [
                {
                    **dataclasses.asdict(test),
                    "evidence": [
                        dataclasses.asdict(item) for item in test.evidence
                    ],
                }
                for test in result.tests
            ],
            "overall": result.overall,
            "overall_reason": result.overall_reason,
            "rules": {
                "PASS": "the stated criterion is met on the cited evidence",
                "FAIL": "the stated criterion is violated",
                "INDETERMINATE": (
                    "the available evidence cannot support either conclusion"
                ),
                "missing_data": "missing data is never a PASS",
                "ordering": (
                    "later tests remain informative when an earlier one is "
                    "unresolved, but cannot establish overall clearance"
                ),
            },
        }
        print(f"\nwrote {_write_json(args.manifest, payload)}")


if __name__ == "__main__":
    main()
