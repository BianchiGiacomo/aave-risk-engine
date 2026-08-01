"""Reusable real-market analyses for the Streamlit dashboard."""

from __future__ import annotations

import os
from dataclasses import replace

import numpy as np

from .config import SimConfig, V4Liquidation
from .data import arfc_clearance_test, build_real_book
from .data.book import scenario_config_from_snapshot
from .data.episodes import (
    EPISODES,
    episode_path_file,
    episode_scenarios,
    load_episode_paths,
    rolling_windows,
)
from .engine import RiskEngine, evaluate_book
from .multiperiod import simulate_multi_period


def _book(snapshot, scope: str, min_target_share: float):
    if scope not in {"USD debt", "Combined"}:
        raise ValueError(f"unknown book scope {scope!r}")
    return build_real_book(
        snapshot,
        min_target_share=min_target_share,
        model_eth_debt=scope == "Combined",
    )


def _config(snapshot, n_scenarios: int, seed: int):
    return replace(
        scenario_config_from_snapshot(snapshot),
        sim=SimConfig(n_scenarios=n_scenarios, seed=seed, chunk_size=2_000),
    )


def _variants(config, ordered: bool = False):
    bonus = config.risk.liquidation_bonus
    max_bonus = round(bonus * 1.11, 4)
    base = replace(config.risk, ordered_queue=ordered)
    return {
        "V3": base,
        "V4 Main (HF 1.24)": replace(
            base,
            v4=V4Liquidation(
                target_health_factor=1.24,
                max_bonus=max_bonus,
                liquidation_bonus_factor=0.90,
                hf_max_bonus=0.90,
                close_factor_floor=0.60,
            ),
        ),
        "V4 correlated (HF 1.0137)": replace(
            base,
            v4=V4Liquidation(
                target_health_factor=1.0137,
                max_bonus=max_bonus,
                liquidation_bonus_factor=1.0,
                hf_max_bonus=0.99,
                close_factor_floor=0.35,
            ),
        ),
    }


def run_market_analysis(
    snapshot,
    scope: str,
    min_target_share: float,
    n_scenarios: int,
    seed: int,
    budget_usd: float,
    ordered: bool,
) -> dict:
    """Run the current-book tail risk, cap sweep, and clearance test."""
    config = _config(snapshot, n_scenarios, seed)
    if ordered:
        config = replace(config, risk=replace(config.risk, ordered_queue=True))
    book = _book(snapshot, scope, min_target_share)
    engine = RiskEngine(config)
    result = engine.run(book=book)
    recommendation = engine.recommend_cap(
        budget_usd=budget_usd,
        cap_min=book.total_debt * 0.1,
        cap_max=book.total_debt * 3.0,
        n_grid=18,
        book=book,
    )
    clearance = (
        arfc_clearance_test(snapshot, min_target_share=min_target_share)
        if snapshot.depth is not None
        else None
    )
    return {
        "config": config,
        "book": book,
        "engine": engine,
        "result": result,
        "recommendation": recommendation,
        "clearance": clearance,
    }


def run_v4_analysis(
    snapshot,
    scope: str,
    min_target_share: float,
    n_scenarios: int,
    seed: int,
) -> list[dict]:
    """Compare V3 and V4 mechanics under aggregate and ordered clearing."""
    config = _config(snapshot, n_scenarios, seed)
    book = _book(snapshot, scope, min_target_share)
    engine = RiskEngine(config)
    rows = []
    for mechanics, risk in _variants(config).items():
        for queue, mode_risk in (
            ("Aggregate", risk),
            ("Ordered", replace(risk, ordered_queue=True)),
        ):
            result = engine.run(book=book, risk=mode_risk)
            rows.append(
                {
                    "Mechanics": mechanics,
                    "Queue": queue,
                    "P(bad debt)": result.prob_bad_debt,
                    "Mean bad debt": result.mean,
                    "VaR99": result.var,
                    "CVaR99": result.cvar,
                }
            )
    return rows


def run_episode_analysis(
    snapshot,
    scope: str,
    min_target_share: float,
    stressed_haircut: float = 0.5,
) -> list[dict]:
    """Replay committed historical paths through the selected current book."""
    if snapshot.reserve.symbol.lower() != "wsteth":
        raise ValueError("episode replay is calibrated for the wstETH/ETH market")
    config = scenario_config_from_snapshot(snapshot)
    book = _book(snapshot, scope, min_target_share)
    horizon = max(1, int(round(config.stress.horizon_days)))
    names = sorted(name for name in EPISODES if os.path.exists(episode_path_file(name)))
    rows = []
    for name in names:
        paths = load_episode_paths(name)
        windows = rolling_windows(paths, horizon)
        for depth_label, haircut in (("Quiet", 0.0), ("50% haircut", stressed_haircut)):
            scenarios = episode_scenarios(paths, config, depth_haircut=haircut)
            result = evaluate_book(
                book,
                scenarios,
                config.risk,
                config.stress.liquidation_delay_drawdown,
                config.sim.cvar_level,
                chunk_size=config.sim.chunk_size,
                depth_points=config.liquidity.depth_points,
            )
            index = int(np.argmax(result.bad_debt))
            rows.append(
                {
                    "Episode": name,
                    "Depth": depth_label,
                    "Worst bad debt": result.worst,
                    "Window": windows["start_dates"][index] if result.worst > 0 else "none",
                    "ETH return": float(windows["eth_return"][index]),
                    "Peg drop": float(windows["peg_drop"][index]),
                }
            )
    return rows


def run_multiperiod_analysis(
    snapshot,
    scope: str,
    min_target_share: float,
    n_paths: int,
    seed: int,
    n_periods: int,
    total_days: float,
    replenish: float,
) -> list[dict]:
    """Compare terminal shocks with evolving books for V3 and V4."""
    config = _config(snapshot, n_paths, seed)
    book = _book(snapshot, scope, min_target_share)
    single_config = replace(config, stress=replace(config.stress, horizon_days=total_days))
    rows = []
    for mechanics, risk in _variants(config, ordered=True).items():
        single = RiskEngine(replace(single_config, risk=risk)).run(book=book)
        multi = simulate_multi_period(
            replace(config, risk=risk),
            book,
            n_periods=n_periods,
            total_days=total_days,
            replenish=replenish,
            n_paths=n_paths,
            seed=seed,
        )
        rows.extend(
            [
                {
                    "Mechanics": mechanics,
                    "Path": "Single shock",
                    "P(bad debt)": single.prob_bad_debt,
                    "Mean bad debt": single.mean,
                    "CVaR99": single.cvar,
                    "P(reliquidation)": 0.0,
                    "Events per path": 0.0,
                },
                {
                    "Mechanics": mechanics,
                    "Path": "Multi-period",
                    "P(bad debt)": multi.prob_bad_debt,
                    "Mean bad debt": multi.mean,
                    "CVaR99": multi.cvar,
                    "P(reliquidation)": multi.prob_reliquidation,
                    "Events per path": multi.mean_events,
                },
            ]
        )
    return rows


def account_rows(snapshot, min_target_share: float) -> list[dict]:
    """Return target-dominant borrower records for concentration display."""
    rows = []
    for account in snapshot.accounts:
        if account.debt_usd < 10_000 or account.target_share < min_target_share:
            continue
        rows.append(
            {
                "Account": account.address,
                "Debt": account.debt_usd,
                "Collateral": account.collateral_usd,
                "Target share": account.target_share,
                "ETH debt share": account.eth_debt_share,
                "Health factor": account.health_factor,
            }
        )
    return sorted(rows, key=lambda row: row["Debt"], reverse=True)
