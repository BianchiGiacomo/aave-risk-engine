"""Replay historical stress episodes through today's real book.

Usage:
    python -m aave_risk_engine.run_episode_replay [--snapshot path] [names ...]

For each episode, every rolling stress-horizon window of the realized ETH
and stETH/ETH paths becomes one deterministic scenario, evaluated against
the current snapshot's borrower book and depth curve. This is scenario
replay on today's book, not a reconstruction of the historical book.
"""

from __future__ import annotations

import argparse
import os

import numpy as np

from .data import build_real_book, load_snapshot
from .data.book import scenario_config_from_snapshot
from .data.episodes import EPISODES, episode_path_file, episode_scenarios, load_episode_paths, rolling_windows
from .engine import evaluate_book


def _fmt(x: float) -> str:
    for unit, div in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(x) >= div:
            return f"${x / div:,.2f}{unit}"
    return f"${x:,.0f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", default=[], help="episode names (default: all committed)")
    parser.add_argument("--snapshot", default=None)
    parser.add_argument("--stressed-haircut", type=float, default=0.5)
    parser.add_argument("--min-target-share", type=float, default=0.5)
    args = parser.parse_args()

    snapshot = load_snapshot(args.snapshot)
    config = scenario_config_from_snapshot(snapshot)
    stress = config.stress
    horizon = max(1, int(round(stress.horizon_days)))

    usd_book = build_real_book(snapshot, min_target_share=args.min_target_share)
    combined_book = build_real_book(
        snapshot, min_target_share=args.min_target_share, model_eth_debt=True
    )

    print(
        f"Historical episode replay | {snapshot.reserve.symbol} ({snapshot.chain}) "
        f"block {snapshot.block:,} | horizon {horizon}d"
    )
    print(
        f"books: USD-debt {_fmt(usd_book.total_debt)} "
        f"| combined {_fmt(combined_book.total_debt)} "
        f"(ETH-denominated {_fmt(float(combined_book.eth_debt_usd.sum()))})"
    )
    print(
        "note: replay applies realized market paths to today's book and"
        " today's depth curve; the historical borrower book is not"
        " reconstructed."
    )

    names = args.names or sorted(
        n for n in EPISODES if os.path.exists(episode_path_file(n))
    )
    for name in names:
        paths = load_episode_paths(name)
        windows = rolling_windows(paths, horizon)
        eth_return = windows["eth_return"]
        peg_drop = windows["peg_drop"]
        worst_eth = int(np.argmin(eth_return))
        worst_peg = int(np.argmax(peg_drop))

        print(f"\nEpisode: {name} ({paths.dates[0]} to {paths.dates[-1]})")
        print(f"  {paths.meta.get('description', '')}")
        print(
            f"  worst {horizon}d ETH return: {eth_return[worst_eth]:+.1%} "
            f"({windows['start_dates'][worst_eth]}) "
            f"| worst {horizon}d peg drop: {peg_drop[worst_peg]:.2%} "
            f"({windows['start_dates'][worst_peg]})"
        )

        downside = max(0.0, -float(eth_return[worst_eth]))
        model_peg = min(
            stress.base_peg_drop + stress.peg_crash_beta * downside, stress.max_peg_drop
        )
        print(
            f"  peg law check at that ETH move: model {model_peg:.2%} "
            f"vs realized {peg_drop[worst_eth]:.2%} in the same window"
        )

        top_windows = None
        for label, book in (("USD-debt book", usd_book), ("combined book", combined_book)):
            line = f"  {label:<14}:"
            for depth_label, haircut in (("quiet", 0.0), ("stressed", args.stressed_haircut)):
                scen = episode_scenarios(paths, config, depth_haircut=haircut)
                res = evaluate_book(
                    book,
                    scen,
                    config.risk,
                    stress.liquidation_delay_drawdown,
                    config.sim.cvar_level,
                    chunk_size=config.sim.chunk_size,
                    depth_points=config.liquidity.depth_points,
                )
                idx = int(np.argmax(res.bad_debt))
                when = windows["start_dates"][idx] if res.worst > 0 else "none"
                line += (
                    f"  {depth_label} depth: max bad debt {_fmt(res.worst):>9}"
                    + (f" ({when})" if res.worst > 0 else "")
                )
                if label == "combined book" and depth_label == "quiet":
                    top_windows = np.argsort(res.bad_debt)[::-1][:3]
                    top_bad = res.bad_debt
            print(line)

        if top_windows is not None and top_bad[top_windows[0]] > 0:
            drivers = ", ".join(
                f"{windows['start_dates'][i]} (eth {eth_return[i]:+.1%}, "
                f"peg {peg_drop[i]:.2%}) {_fmt(top_bad[i])}"
                for i in top_windows
                if top_bad[i] > 0
            )
            print(f"  driving windows (combined, quiet): {drivers}")


if __name__ == "__main__":
    main()
