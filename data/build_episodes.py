"""Fetch and commit historical episode price paths.

Usage:
    python -m aave_risk_engine.data.build_episodes [names ...]

With no arguments, all registered episodes are fetched. Paths are written
to data/episodes/ so replays run offline and deterministically.
"""

from __future__ import annotations

import argparse

from .episodes import EPISODES, fetch_episode_paths, save_episode_paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", default=[], help="episode names (default: all)")
    args = parser.parse_args()

    names = args.names or sorted(EPISODES)
    for name in names:
        if name not in EPISODES:
            parser.error(f"unknown episode {name!r}; known: {sorted(EPISODES)}")
        episode = EPISODES[name]
        print(f"fetching {name} ({episode.start} to {episode.end}) ...")
        paths = fetch_episode_paths(episode)
        file = save_episode_paths(paths)
        lo = min(paths.driver_usd)
        hi = max(paths.driver_usd)
        line = f"  {len(paths.dates)} days | driver {hi:,.0f} to {lo:,.0f} USD"
        if paths.ratio:
            line += f" | min ratio {min(paths.ratio):.4f}"
        print(line)
        print(f"  wrote {file}")


if __name__ == "__main__":
    main()
