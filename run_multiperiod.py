"""Multi-period simulation on the real book: V3 vs V4 with re-liquidation.

Usage:
    python -m aave_risk_engine.run_multiperiod [--snapshot path]

Compares each liquidation-mechanics variant in two ways over the same
total stress window: one single-period shock (ordered clearing, stalls
marked immediately) versus a multi-period path where books evolve,
positions can be re-liquidated, stalled liquidations wait, and depth
replenishes between periods.
"""

from __future__ import annotations

import argparse
from dataclasses import replace

from .config import SimConfig, V4Liquidation
from .data import build_real_book, load_snapshot
from .data.book import scenario_config_from_snapshot
from .engine import RiskEngine
from .multiperiod import simulate_multi_period


def _fmt(x: float) -> str:
    for unit, div in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(x) >= div:
            return f"${x / div:,.2f}{unit}"
    return f"${x:,.0f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default=None)
    parser.add_argument("--periods", type=int, default=8)
    parser.add_argument("--days", type=float, default=4.0)
    parser.add_argument("--replenish", type=float, default=1.0)
    parser.add_argument("--min-target-share", type=float, default=0.5)
    parser.add_argument("--n-paths", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    snapshot = load_snapshot(args.snapshot)
    config = scenario_config_from_snapshot(snapshot)
    config.sim = SimConfig(n_scenarios=args.n_paths, seed=args.seed, chunk_size=2_000)

    v3_bonus = config.risk.liquidation_bonus
    max_bonus = round(v3_bonus * 1.11, 4)
    base_risk = replace(config.risk, ordered_queue=True)
    variants = {
        "V3": base_risk,
        "V4 Main (1.24)": replace(
            base_risk,
            v4=V4Liquidation(
                target_health_factor=1.24,
                max_bonus=max_bonus,
                liquidation_bonus_factor=0.90,
                hf_max_bonus=0.90,
                close_factor_floor=0.60,
            ),
        ),
        "V4 corr (1.0137)": replace(
            base_risk,
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

    dt = args.days / args.periods
    print(
        f"Multi-period simulation | {snapshot.reserve.symbol} ({snapshot.chain}) "
        f"block {snapshot.block:,} | {args.periods} x {dt:.2g}d = {args.days:.2g}d window "
        f"| depth replenish {args.replenish:.0%}"
    )
    print(
        "single rows: one shock over the whole window, ordered clearing,"
        " stalls marked immediately. multi rows: evolving book,"
        " re-liquidation, stalls wait until the window ends."
    )

    single_config = replace(
        config, stress=replace(config.stress, horizon_days=args.days)
    )
    for book_label, book in (
        (f"USD-debt book ({_fmt(usd_book.total_debt)})", usd_book),
        (f"combined book ({_fmt(combined_book.total_debt)})", combined_book),
    ):
        print(f"\n{book_label}")
        for label, risk in variants.items():
            engine = RiskEngine(replace(single_config, risk=risk))
            single = engine.run(book=book)
            print(
                f"  {label:<17} single: P(bad debt) {single.prob_bad_debt:6.2%} "
                f"| mean {_fmt(single.mean):>8} | CVaR99 {_fmt(single.cvar):>9}"
            )
            multi = simulate_multi_period(
                replace(config, risk=risk),
                book,
                n_periods=args.periods,
                total_days=args.days,
                replenish=args.replenish,
            )
            marks_share = (
                multi.terminal_marks.sum() / multi.bad_debt.sum()
                if multi.bad_debt.sum() > 0
                else 0.0
            )
            print(
                f"  {label:<17} multi : P(bad debt) {multi.prob_bad_debt:6.2%} "
                f"| mean {_fmt(multi.mean):>8} | CVaR99 {_fmt(multi.cvar):>9} "
                f"| marks {marks_share:.0%} of losses "
                f"| events/path {multi.mean_events:.2f} "
                f"| P(reliq) {multi.prob_reliquidation:.2%}"
            )


if __name__ == "__main__":
    main()
