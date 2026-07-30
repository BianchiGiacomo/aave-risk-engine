"""Compare V3 and V4 liquidation mechanics on the same real book.

Usage:
    python -m aave_risk_engine.run_v4_comparison [--snapshot path]

Everything except the liquidation mechanics is held fixed: same borrower
book, same calibrated scenarios, same depth curve. V4 parameters follow
the mainnet activation values: repayment sized to the Spoke's target
health factor, a dynamic bonus rising to 1.11x the V3 bonus, and the
launch close-factor floors.
"""

from __future__ import annotations

import argparse
from dataclasses import replace

from .config import SimConfig, V4Liquidation
from .data import build_real_book, load_snapshot
from .data.book import scenario_config_from_snapshot
from .engine import RiskEngine


def _fmt(x: float) -> str:
    for unit, div in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(x) >= div:
            return f"${x / div:,.2f}{unit}"
    return f"${x:,.0f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default=None)
    parser.add_argument("--budget", type=float, default=5e6)
    parser.add_argument("--min-target-share", type=float, default=0.5)
    parser.add_argument("--n-scenarios", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    snapshot = load_snapshot(args.snapshot)
    config = scenario_config_from_snapshot(snapshot)
    config.sim = SimConfig(n_scenarios=args.n_scenarios, seed=args.seed, chunk_size=2_000)
    engine = RiskEngine(config)

    v3_bonus = config.risk.liquidation_bonus
    max_bonus = round(v3_bonus * 1.11, 4)
    variants = {
        "V3 (on-chain params)": config.risk,
        "V4 Main Spoke": replace(
            config.risk,
            v4=V4Liquidation(
                target_health_factor=1.24,
                max_bonus=max_bonus,
                liquidation_bonus_factor=0.90,
                hf_max_bonus=0.90,
                close_factor_floor=0.60,
            ),
        ),
        "V4 correlated Spoke": replace(
            config.risk,
            v4=V4Liquidation(
                target_health_factor=1.0137,
                max_bonus=max_bonus,
                liquidation_bonus_factor=1.0,
                hf_max_bonus=0.99,
                close_factor_floor=0.35,
            ),
        ),
    }

    usd_book = build_real_book(snapshot, min_target_share=args.min_target_share)
    combined_book = build_real_book(
        snapshot, min_target_share=args.min_target_share, model_eth_debt=True
    )

    print(
        f"V3 vs V4 liquidation mechanics | {snapshot.reserve.symbol} "
        f"({snapshot.chain}) block {snapshot.block:,}"
    )
    print(
        f"held fixed: book, scenarios, depth | V3 bonus {v3_bonus:.2%} "
        f"-> V4 max bonus {max_bonus:.2%} (1.11x)"
    )
    print(
        "V4 ARFC params: Main target HF 1.24, bonus factor 0.90,"
        " hfForMaxBonus 0.90, floor 0.60 | correlated target 1.0137,"
        " bonus factor 1.0, hfForMaxBonus 0.99, floor 0.35."
    )

    for book_label, book in (
        (f"USD-debt book ({_fmt(usd_book.total_debt)})", usd_book),
        (f"combined book ({_fmt(combined_book.total_debt)})", combined_book),
    ):
        print(f"\n{book_label}")
        for label, risk in variants.items():
            r = engine.run(book=book, risk=risk)
            print(
                f"  {label:<21}: P(bad debt) {r.prob_bad_debt:6.2%} "
                f"| mean {_fmt(r.mean):>8} | VaR99 {_fmt(r.var):>9} "
                f"| CVaR99 {_fmt(r.cvar):>9}"
            )

    print(f"\nModel-safe exposure at CVaR99 budget {_fmt(args.budget)} (USD-debt book)")
    for label in ("V3 (on-chain params)", "V4 Main Spoke"):
        rec = engine.recommend_cap(
            budget_usd=args.budget,
            cap_min=usd_book.total_debt * 0.1,
            cap_max=usd_book.total_debt * 3.0,
            n_grid=18,
            book=usd_book,
            risk=variants[label],
        )
        print(f"  {label:<21}: {_fmt(rec['recommended_cap'])}")


if __name__ == "__main__":
    main()
