"""Measure the simultaneous liquidation requirement on the published stress path.

The run rebuilds the scenario set of a pinned market report from the same
snapshot, seed, and scenario count, and first checks that it reproduces
that report's combined-book CVaR. Only then does it size the liquidations,
so the stress path is demonstrably the published one rather than a
description of it.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

from .config import SimConfig
from .data.book import build_real_book, scenario_config_from_snapshot
from .data.snapshot import load_snapshot
from .engine import RiskEngine
from .simultaneous_requirement import compute

_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_SNAPSHOT = _PACKAGE_DIR / "data" / "snapshots" / "aave_v3_ethereum_wsteth.json"
_DEFAULT_MARKET = _PACKAGE_DIR / "docs" / "manifests" / "ethereum-wsteth-2026-08-18.json"
_REPRODUCTION_TOLERANCE = 1e-9


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


def _m(value: float) -> str:
    return f"${value / 1e6:,.2f}m"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default=str(_DEFAULT_SNAPSHOT))
    parser.add_argument("--market-manifest", default=str(_DEFAULT_MARKET))
    parser.add_argument("--manifest")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    snapshot_path = Path(args.snapshot).resolve()
    market_path = Path(args.market_manifest).resolve()
    market = json.loads(market_path.read_text(encoding="utf-8"))

    if _sha256(snapshot_path) != market["snapshot"]["sha256"]:
        raise SystemExit("snapshot does not match the market report manifest")

    run = market["run"]
    snapshot = load_snapshot(str(snapshot_path))
    config = scenario_config_from_snapshot(snapshot)
    config.sim = SimConfig(
        n_scenarios=run["n_scenarios"], seed=run["seed"], chunk_size=2_000
    )
    book = build_real_book(
        snapshot, min_target_share=run["min_target_share"], model_eth_debt=True
    )
    engine = RiskEngine(config)

    reproduced = engine.run(book=book).cvar
    published = market["books"]["combined"]["metrics"]["CVaR_99%"]
    reproduces = abs(reproduced - published) <= _REPRODUCTION_TOLERANCE * max(
        1.0, abs(published)
    )
    if not reproduces:
        raise SystemExit(
            f"scenario set does not reproduce the published combined CVaR99: "
            f"{reproduced} vs {published}"
        )

    result = compute(book, engine.scenarios, config.risk, tail_level=run["cvar_level"])
    horizon = config.stress.horizon_days
    static_bound = market["clearance"]["largest_borrower_usd"]

    print("Simultaneous liquidation requirement (test 2)")
    print(
        f"  snapshot          : block {market['snapshot']['block']:,} | "
        f"{result.accounts} accounts | debt {_m(result.book_debt_usd)}"
    )
    print(
        f"  stress path       : published market report, {result.n_scenarios:,} "
        f"scenarios, seed {run['seed']}, {horizon:g}-day horizon"
    )
    print(
        f"  reproduction      : combined CVaR99 {reproduced:,.2f} "
        f"= published {published:,.2f}"
    )
    print(f"  P(any liquidation): {result.prob_any_liquidation:.2%}")
    print(
        "\n  per scenario             p50        p90        p95        p99"
        "        max  tail mean"
    )
    for label, dist in (
        ("repayment (debt)", result.repayment_usd),
        ("seizure (collateral)", result.seizure_usd),
    ):
        print(
            f"  {label:<20} "
            + " ".join(
                f"{_m(v):>10}"
                for v in (dist.p50, dist.p90, dist.p95, dist.p99, dist.maximum, dist.tail_mean)
            )
        )
    count = result.liquidatable_positions
    print(
        f"  {'positions':<20} "
        + " ".join(
            f"{v:>10.0f}"
            for v in (count.p50, count.p90, count.p95, count.p99, count.maximum)
        )
        + f" {count.tail_mean:>10.1f}"
    )
    print(
        f"\n  tail scenarios ({result.tail_scenarios}): the single largest "
        f"repayment is on average {result.largest_share_of_tail_repayment:.1%} "
        "of the total"
    )
    print(
        f"  static bound for comparison: full seizure of the largest position "
        f"{_m(static_bound)} (collateral, taken from the clearance test)"
    )

    if args.manifest:
        payload = {
            "schema_version": 1,
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "command": [
                "python",
                "-m",
                "aave_risk_engine.run_simultaneous_requirement",
                *(argument.replace("\\", "/") for argument in sys.argv[1:]),
            ],
            "snapshot": {
                "file": os.path.relpath(snapshot_path, _PACKAGE_DIR).replace("\\", "/"),
                "sha256": _sha256(snapshot_path),
                "chain": market["snapshot"]["chain"],
                "block": market["snapshot"]["block"],
                "timestamp": market["snapshot"]["timestamp"],
                "asset": market["snapshot"]["asset"],
            },
            "stress_path": {
                "source_manifest": os.path.relpath(market_path, _PACKAGE_DIR).replace(
                    "\\", "/"
                ),
                "source_manifest_sha256": _sha256(market_path),
                "n_scenarios": run["n_scenarios"],
                "seed": run["seed"],
                "horizon_days": horizon,
                "book": "combined, ETH-denominated debt modelled",
                "reproduced_combined_cvar99": reproduced,
                "published_combined_cvar99": published,
                "reproduces_published_scenarios": reproduces,
            },
            "definitions": {
                "repayment_usd": "debt repaid in the first liquidation round, "
                "summed over positions with health factor below one",
                "seizure_usd": "collateral received for that repayment, "
                "repayment times one plus the bonus",
                "sizing": "V3 close factor: full below the full-liquidation "
                "health factor, otherwise the close factor; capped by "
                "seizable collateral",
                "tail": "the worst (1 - cvar_level) share of scenarios ranked "
                "by total repayment",
            },
            "results": {
                **{
                    key: value
                    for key, value in dataclasses.asdict(result).items()
                    if key not in ("repayment_usd", "seizure_usd", "liquidatable_positions")
                },
                "repayment_usd": dataclasses.asdict(result.repayment_usd),
                "seizure_usd": dataclasses.asdict(result.seizure_usd),
                "liquidatable_positions": dataclasses.asdict(
                    result.liquidatable_positions
                ),
                "static_bound_largest_full_seizure_usd": static_bound,
            },
            "not_modelled": [
                "a second liquidation round inside the horizon",
                "positions outside the target-dominant combined book",
                "path dependence inside the horizon; the stress is a "
                "single horizon shock, as in the published report",
            ],
        }
        print(f"\nwrote {_write_json(args.manifest, payload)}")


if __name__ == "__main__":
    main()
