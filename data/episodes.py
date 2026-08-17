"""Historical stress episodes: data, rolling-window scenarios, and replay.

This is scenario replay, not book reconstruction: the realized price and
peg paths of past episodes are rolled through a selected snapshot book and
depth curve. Reconstructing the historical borrower book would need
archive-node state, which keyless public endpoints do not serve; the
replay answers the complementary question of what those market paths
would do to the positions in that snapshot.

Price history comes from the keyless DefiLlama coins API, which serves
daily prices back past 2022.

Episodes are named after the historical event, but the scenarios carry
only the collateral-relevant paths: the ETH price and the stETH/ETH
ratio. Liability-side effects of an event, such as the March 2023 USDC
depeg itself, are not modeled by the replay.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.request
from dataclasses import dataclass, field

import numpy as np

from ..config import ScenarioConfig
from ..slippage import calibrate_liquidity
from ..stress import Scenarios
from .markets import rolling_median3 as _rolling_median3

_HEADERS = {"User-Agent": "aave-risk-engine/0.1"}

_ETH_KEY = "coingecko:ethereum"
_STETH_KEY = "ethereum:0xae7ab96520DE3A18E5E111B5EaAb095312D7fE84"


@dataclass(frozen=True)
class Episode:
    """A named historical stress window."""

    name: str
    start: str  # ISO date, inclusive
    end: str  # ISO date, inclusive
    description: str
    # DefiLlama coin keys: the USD price driver and, optionally, an LST
    # whose USD price yields the LST/driver peg ratio.
    driver_key: str = _ETH_KEY
    ratio_key: str | None = _STETH_KEY


EPISODES: dict[str, Episode] = {
    "steth-depeg-2022": Episode(
        name="steth-depeg-2022",
        start="2022-05-01",
        end="2022-07-01",
        description=(
            "Terra collapse, Celsius freeze, and 3AC unwind: ETH roughly"
            " halves and stETH/ETH trades down to about 0.94."
        ),
    ),
    "ftx-2022": Episode(
        name="ftx-2022",
        start="2022-11-01",
        end="2022-12-01",
        description=(
            "FTX failure: fast ETH drawdown. The stETH peg stayed tight"
            " (worst two-day drop under 2%) and held through the"
            " loss-driving crash window."
        ),
    ),
    "usdc-depeg-2023": Episode(
        name="usdc-depeg-2023",
        start="2023-03-01",
        end="2023-03-21",
        description=(
            "SVB weekend market window: ETH dips and recovers, stETH/ETH"
            " stays tight. The USDC liability depeg itself is not modeled;"
            " this window supplies only the ETH and stETH/ETH paths."
        ),
    ),
}


@dataclass
class EpisodePaths:
    """Aligned daily series for one episode."""

    name: str
    dates: list[str]
    driver_usd: list[float]
    ratio: list[float] | None = None
    source: str = "defillama"
    fetched_at: int = 0
    meta: dict = field(default_factory=dict)


def episodes_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "episodes")


def episode_path_file(name: str) -> str:
    return os.path.join(episodes_dir(), f"{name}.json")


def _llama_chart(keys: list[str], start_ts: int, span_days: int, timeout: float = 30.0) -> dict:
    joined = ",".join(keys)
    url = f"https://coins.llama.fi/chart/{joined}?start={start_ts}&span={span_days}&period=1d"
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_episode_paths(episode: Episode, timeout: float = 30.0) -> EpisodePaths:
    """Fetch and day-align the episode's price series from DefiLlama."""
    start = dt.datetime.fromisoformat(episode.start).replace(tzinfo=dt.timezone.utc)
    end = dt.datetime.fromisoformat(episode.end).replace(tzinfo=dt.timezone.utc)
    span = (end - start).days + 1

    keys = [episode.driver_key] + ([episode.ratio_key] if episode.ratio_key else [])
    out = _llama_chart(keys, int(start.timestamp()), span, timeout)
    coins = out.get("coins", {})

    def by_day(key: str) -> dict[str, float]:
        series = coins.get(key, {}).get("prices", [])
        return {
            dt.datetime.fromtimestamp(p["timestamp"], dt.timezone.utc).date().isoformat(): float(
                p["price"]
            )
            for p in series
        }

    driver = by_day(episode.driver_key)
    ratio_usd = by_day(episode.ratio_key) if episode.ratio_key else {}
    days = sorted(driver)
    if episode.ratio_key:
        days = sorted(set(days) & set(ratio_usd))
    if len(days) < 5:
        raise RuntimeError(f"too few aligned days for episode {episode.name}")

    return EpisodePaths(
        name=episode.name,
        dates=days,
        driver_usd=[driver[d] for d in days],
        ratio=[ratio_usd[d] / driver[d] for d in days] if episode.ratio_key else None,
        fetched_at=int(dt.datetime.now(dt.timezone.utc).timestamp()),
        meta={"description": episode.description},
    )


def save_episode_paths(paths: EpisodePaths) -> str:
    os.makedirs(episodes_dir(), exist_ok=True)
    file = episode_path_file(paths.name)
    with open(file, "w", encoding="utf-8") as fh:
        json.dump(paths.__dict__, fh, indent=1)
    return file


def load_episode_paths(name: str) -> EpisodePaths:
    with open(episode_path_file(name), encoding="utf-8") as fh:
        return EpisodePaths(**json.load(fh))


def _to_daily_grid(dates: list[str], values: list[float]) -> tuple[list[str], np.ndarray]:
    """Reindex a gappy daily series onto a full calendar grid.

    Public daily marks can skip days; without regridding, an H-day rolling
    window would silently span more than H calendar days. Gaps are filled
    by linear interpolation.
    """
    days = [dt.date.fromisoformat(d) for d in dates]
    ordinals = np.array([d.toordinal() for d in days], dtype=float)
    full = np.arange(ordinals[0], ordinals[-1] + 1)
    grid = np.interp(full, ordinals, np.asarray(values, dtype=float))
    grid_dates = [dt.date.fromordinal(int(o)).isoformat() for o in full]
    return grid_dates, grid




def rolling_windows(paths: EpisodePaths, horizon_days: int, smooth_ratio: bool = True) -> dict:
    """H-day returns and peg drops for every start day in the episode."""
    grid_dates, driver = _to_daily_grid(paths.dates, paths.driver_usd)
    if driver.size <= horizon_days:
        raise ValueError("episode shorter than the stress horizon")
    eth_return = driver[horizon_days:] / driver[:-horizon_days] - 1.0

    if paths.ratio is not None:
        _, ratio = _to_daily_grid(paths.dates, paths.ratio)
        # An LST/underlying ratio has a hard ceiling at par: one unit of the
        # underlying always mints one unit of the LST, so sustained prints
        # above 1.0 are venue or timestamp artifacts, not market prices.
        # The committed 2022 stETH series prints up to 1.12 on chaotic days;
        # without the cap, an artifact-high print reverting to normal shows
        # up as a fake double-digit two-day depeg.
        ratio = np.minimum(ratio, 1.0)
        if smooth_ratio:
            ratio = _rolling_median3(ratio)
        rel = ratio[horizon_days:] / ratio[:-horizon_days]
        peg_drop = np.maximum(0.0, 1.0 - rel)
    else:
        peg_drop = np.zeros_like(eth_return)

    return {
        "start_dates": grid_dates[: eth_return.size],
        "eth_return": eth_return,
        "peg_drop": peg_drop,
    }


def episode_scenarios(
    paths: EpisodePaths,
    config: ScenarioConfig,
    depth_haircut: float = 0.0,
) -> Scenarios:
    """One deterministic scenario per rolling window of the episode.

    Collateral repricing follows the engine's convention: spot times the
    window's driver return times one minus the window's peg drop. Depth is
    the snapshot's calibrated curve with a flat haircut, since historical depth is
    not observable from keyless sources.
    """
    horizon = max(1, int(round(config.stress.horizon_days)))
    windows = rolling_windows(paths, horizon)
    eth_return = windows["eth_return"]
    peg_drop = windows["peg_drop"]
    n = eth_return.size

    coll_price = np.maximum(
        config.asset.spot_price * (1.0 + eth_return) * (1.0 - peg_drop), 1e-9
    )
    l0 = calibrate_liquidity(
        config.asset.spot_price,
        config.liquidity.ref_notional_usd,
        config.liquidity.ref_slippage,
    )
    return Scenarios(
        coll_price=coll_price,
        depth_liquidity=np.full(n, l0 * (1.0 - depth_haircut)),
        eth_return=eth_return,
        peg_drop=peg_drop,
        depth_haircut=np.full(n, depth_haircut),
    )
