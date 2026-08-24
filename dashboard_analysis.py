"""Reusable real-market analyses for the Streamlit dashboard."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

import numpy as np

from .config import SimConfig, V4Liquidation
from .data import (
    arfc_clearance_test,
    build_real_book,
    max_notional_at_slippage,
)
from .data.book import scenario_config_from_snapshot
from .data.episodes import (
    EPISODES,
    episode_path_file,
    episode_scenarios,
    load_episode_paths,
    rolling_windows,
)
from .engine import RiskEngine, evaluate_book
from .liquidator_balance_sheet import (
    LiquidatorAssumptions,
    minimum_liquidation_bonus,
    simulate_liquidator_balance_sheet,
)
from .multiperiod import (
    sample_stress_paths,
    simulate_multi_period,
    terminal_scenarios_from_paths,
)
from .time_to_exit import ExitAssumptions, build_exit_curve


@dataclass(frozen=True)
class ClearanceExtensionInputs:
    """Explicit dashboard sensitivities for delayed liquidation recovery."""

    decision_horizon_days: float = 7.0
    stress_depth_haircut: float = 0.50
    quiet_refill_hours: float = 6.0
    stressed_refill_hours: float = 24.0
    quiet_redemption_usd_per_day: float = 25_000_000.0
    stressed_redemption_usd_per_day: float = 25_000_000.0
    redemption_delay_hours: float = 24.0
    stalled_drawdown: float = 0.10
    funding_annual_rate: float = 0.10
    hurdle_annual_rate: float = 0.10
    hedge_entry_cost: float = 0.001
    hedge_carry_annual_rate: float = 0.02
    dex_execution_loss: float = 0.01
    redemption_loss: float = 0.0
    canonical_loss: float = 0.04
    dex_market_discount: float = 0.0
    route_strategy: str = "profit_maximizing"
    max_horizon_days: float = 365.0

    def __post_init__(self) -> None:
        if self.decision_horizon_days <= 0.0:
            raise ValueError("decision horizon must be positive")
        if self.quiet_refill_hours <= 0.0 or self.stressed_refill_hours <= 0.0:
            raise ValueError("DEX refill hours must be positive")
        if not 0.0 <= self.stress_depth_haircut < 1.0:
            raise ValueError("stress depth haircut must be in [0, 1)")
        if self.quiet_redemption_usd_per_day < 0.0:
            raise ValueError("quiet redemption throughput must be non-negative")
        if self.stressed_redemption_usd_per_day < 0.0:
            raise ValueError("stressed redemption throughput must be non-negative")
        if self.redemption_delay_hours < 0.0:
            raise ValueError("redemption delay must be non-negative")
        if self.max_horizon_days <= 0.0:
            raise ValueError("maximum horizon must be positive")


def run_clearance_extension(
    snapshot,
    min_target_share: float,
    inputs: ClearanceExtensionInputs,
) -> dict:
    """Evaluate horizon capacity and liquidator economics on one largest sale."""
    clearance = arfc_clearance_test(
        snapshot,
        stressed_haircut=inputs.stress_depth_haircut,
        min_target_share=min_target_share,
    )
    sale = clearance.largest_borrower_usd
    bonus = snapshot.reserve.liquidation_bonus
    debt = sale / (1.0 + bonus)
    strict_quiet_capacity = clearance.max_clearable_usd_quiet
    strict_stressed_capacity = clearance.max_clearable_usd_stressed
    economic_quiet_capacity = max_notional_at_slippage(
        snapshot, inputs.dex_execution_loss
    )
    economic_stressed_capacity = (
        economic_quiet_capacity * (1.0 - inputs.stress_depth_haircut)
    )

    def regimes(quiet_capacity: float, stressed_capacity: float) -> dict:
        values = {
            "Quiet DEX": ExitAssumptions(
                quiet_capacity,
                inputs.quiet_refill_hours,
                stalled_drawdown=inputs.stalled_drawdown,
            ),
            "Stressed DEX": ExitAssumptions(
                stressed_capacity,
                inputs.stressed_refill_hours,
                stalled_drawdown=inputs.stalled_drawdown,
            ),
        }
        if inputs.quiet_redemption_usd_per_day > 0.0:
            values["Quiet + redemption"] = ExitAssumptions(
                quiet_capacity,
                inputs.quiet_refill_hours,
                inputs.quiet_redemption_usd_per_day,
                inputs.redemption_delay_hours,
                inputs.stalled_drawdown,
            )
        if inputs.stressed_redemption_usd_per_day > 0.0:
            values["Stressed + redemption"] = ExitAssumptions(
                stressed_capacity,
                inputs.stressed_refill_hours,
                inputs.stressed_redemption_usd_per_day,
                inputs.redemption_delay_hours,
                inputs.stalled_drawdown,
            )
        return values

    horizon_hours = 24.0 * inputs.decision_horizon_days
    grid = set(float(value) for value in np.linspace(0.0, horizon_hours, 25))
    grid.update(
        value
        for value in (0.0, 1.0, 6.0, 24.0, 72.0, 168.0, horizon_hours)
        if value <= horizon_hours
    )
    plot_horizons = tuple(sorted(grid))
    time_curves = {}
    horizon_rows = []
    for label, assumptions in regimes(
        strict_quiet_capacity, strict_stressed_capacity
    ).items():
        curve = build_exit_curve(
            sale,
            bonus,
            assumptions,
            plot_horizons,
        )
        time_curves[label] = curve
        point = curve.points[-1]
        horizon_rows.append(
            {
                "Regime": label,
                "Capacity at horizon": point.total_capacity_usd,
                "Unresolved sale": point.unresolved_sale_usd,
                "Conditional loss": point.conditional_bad_debt_usd,
                "Estimated clear time": curve.time_to_clear_hours,
                "Pass by horizon": point.passes,
            }
        )

    liquidator_inputs = LiquidatorAssumptions(
        funding_annual_rate=inputs.funding_annual_rate,
        hurdle_annual_rate=inputs.hurdle_annual_rate,
        hedge_entry_cost=inputs.hedge_entry_cost,
        hedge_carry_annual_rate=inputs.hedge_carry_annual_rate,
        dex_execution_loss=inputs.dex_execution_loss,
        redemption_loss=inputs.redemption_loss,
        canonical_loss=inputs.canonical_loss,
        dex_market_discount=inputs.dex_market_discount,
        route_strategy=inputs.route_strategy,
        max_horizon_hours=24.0 * inputs.max_horizon_days,
    )
    economic_rows = []
    for label, assumptions in regimes(
        economic_quiet_capacity, economic_stressed_capacity
    ).items():
        result = simulate_liquidator_balance_sheet(
            debt,
            sale,
            assumptions,
            liquidator_inputs,
        )
        economic_rows.append(
            {
                "Regime": label,
                "Result": result,
                "Minimum bonus": minimum_liquidation_bonus(
                    debt,
                    assumptions,
                    liquidator_inputs,
                ),
            }
        )

    return {
        "clearance": clearance,
        "sale_usd": sale,
        "debt_usd": debt,
        "current_bonus": bonus,
        "strict_instant_capacity_usd": strict_quiet_capacity,
        "economic_instant_capacity_usd": economic_quiet_capacity,
        "time_curves": time_curves,
        "horizon_rows": horizon_rows,
        "economic_rows": economic_rows,
        "inputs": inputs,
    }


def _book(snapshot, scope: str, min_target_share: float):
    if scope not in {"USD debt", "Combined"}:
        raise ValueError(f"unknown book scope {scope!r}")
    return build_real_book(
        snapshot,
        min_target_share=min_target_share,
        model_eth_debt=scope == "Combined",
    )


def _selected_accounts(snapshot, scope: str, min_target_share: float):
    selected = [
        account
        for account in snapshot.accounts
        if account.debt_usd >= 10_000
        and account.collateral_usd > 0
        and account.target_share >= min_target_share
        and (scope == "Combined" or account.eth_debt_share <= 0.5)
        and 0 < account.avg_liquidation_threshold < 1
    ]
    return sorted(selected, key=lambda account: account.debt_usd, reverse=True)


def _config(snapshot, n_scenarios: int, seed: int):
    return replace(
        scenario_config_from_snapshot(snapshot),
        sim=SimConfig(n_scenarios=n_scenarios, seed=seed, chunk_size=2_000),
    )


def _variants(config, ordered: bool = True):
    bonus = config.risk.liquidation_bonus
    max_bonus = round(bonus * 1.11, 4)
    base = replace(config.risk, ordered_queue=ordered)
    return {
        "V3 selected reserve": base,
        "V4 Main Spoke (target HF 1.24)": replace(
            base,
            v4=V4Liquidation(
                target_health_factor=1.24,
                max_bonus=max_bonus,
                liquidation_bonus_factor=0.90,
                hf_max_bonus=0.90,
                close_factor_floor=0.60,
            ),
        ),
        "V4 Correlated Spoke (target HF 1.0137)": replace(
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


def run_liquidation_threshold_sensitivity(
    snapshot,
    scope: str,
    min_target_share: float,
    n_scenarios: int,
    seed: int,
    ordered: bool,
    n_grid: int = 11,
) -> dict:
    """Reprice the current book under counterfactual target-asset LTs.

    Each account's weighted-average LT moves by its target collateral share
    times the change in the reserve LT. This is a current-book transition
    sensitivity, not a model of borrower behavior after a parameter change.
    """
    if n_grid < 2:
        raise ValueError("n_grid must be at least two")
    config = _config(snapshot, n_scenarios, seed)
    if ordered:
        config = replace(config, risk=replace(config.risk, ordered_queue=True))
    book = _book(snapshot, scope, min_target_share)
    accounts = _selected_accounts(snapshot, scope, min_target_share)
    if len(accounts) != book.debt_usd.size:
        raise RuntimeError("account selection does not match the position book")

    current = float(snapshot.reserve.liquidation_threshold)
    lower = max(0.50, current - 0.10)
    upper = min(0.95, current + 0.10)
    thresholds = np.unique(np.append(np.linspace(lower, upper, n_grid), current))
    shares = np.clip(np.array([account.target_share for account in accounts]), 0.0, 1.0)
    engine = RiskEngine(config)
    mean = np.empty(thresholds.size)
    cvar = np.empty(thresholds.size)
    prob = np.empty(thresholds.size)
    underwater = np.empty(thresholds.size, dtype=int)

    for index, threshold in enumerate(thresholds):
        account_lts = np.clip(
            book.lt + shares * (float(threshold) - current),
            1e-6,
            0.999,
        )
        hf0 = (
            book.coll_units
            * config.asset.spot_price
            * account_lts
            / book.debt_usd
        )
        adjusted_book = replace(book, lt=account_lts, hf0=hf0)
        risk = replace(
            config.risk,
            liquidation_threshold=float(threshold),
            ltv=min(config.risk.ltv, float(threshold)),
        )
        result = engine.run(book=adjusted_book, risk=risk)
        mean[index] = result.mean
        cvar[index] = result.cvar
        prob[index] = result.prob_bad_debt
        underwater[index] = int(np.count_nonzero(hf0 < 1.0))

    return {
        "thresholds": thresholds,
        "current_threshold": current,
        "mean": mean,
        "cvar": cvar,
        "prob": prob,
        "underwater_accounts": underwater,
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
    """Compare V3 and V4 mechanics under ordered clearing."""
    config = _config(snapshot, n_scenarios, seed)
    book = _book(snapshot, scope, min_target_share)
    engine = RiskEngine(config)
    rows = []
    for mechanics, risk in _variants(config, ordered=True).items():
        result = engine.run(book=book, risk=risk)
        rows.append(
            {
                "Mechanics": mechanics,
                "P(bad debt)": result.prob_bad_debt,
                "Positive-loss draws": result.positive_loss_count,
                "Mean bad debt": result.mean,
                "Severity if loss": result.conditional_mean_bad_debt,
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
            has_loss = result.worst > 0
            rows.append(
                {
                    "Episode": name,
                    "Depth": depth_label,
                    "Worst bad debt": result.worst,
                    "Window": windows["start_dates"][index] if has_loss else "n/a",
                    "ETH return": float(windows["eth_return"][index]) if has_loss else None,
                    "Peg drop": float(windows["peg_drop"][index]) if has_loss else None,
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
    peg_mean_reversion_speed: float | None = None,
) -> list[dict]:
    """Compare matched terminal shocks with evolving books for V3 and V4."""
    config = _config(snapshot, n_paths, seed)
    if peg_mean_reversion_speed is not None:
        config = replace(
            config,
            stress=replace(
                config.stress,
                peg_mean_reversion_speed=peg_mean_reversion_speed,
            ),
        )
    book = _book(snapshot, scope, min_target_share)
    paths = sample_stress_paths(config, n_periods, total_days, n_paths, seed)
    terminal = terminal_scenarios_from_paths(config, paths)
    rows = []
    for mechanics, risk in _variants(config, ordered=True).items():
        single = evaluate_book(
            book,
            terminal,
            risk,
            config.stress.liquidation_delay_drawdown,
            config.sim.cvar_level,
            chunk_size=config.sim.chunk_size,
            depth_points=config.liquidity.depth_points,
        )
        multi = simulate_multi_period(
            replace(config, risk=risk),
            book,
            n_periods=n_periods,
            total_days=total_days,
            replenish=replenish,
            n_paths=n_paths,
            step_log_returns=paths.step_log_returns,
            peg_idio_steps=paths.peg_idio_steps,
            haircut_idio_steps=paths.haircut_idio_steps,
        )
        rows.extend(
            [
                {
                    "Mechanics": mechanics,
                    "Path": "Single-shock",
                    "P(bad debt)": single.prob_bad_debt,
                    "Positive-loss paths": single.positive_loss_count,
                    "Mean bad debt": single.mean,
                    "CVaR99": single.cvar,
                    "P(reliquidation)": None,
                    "Cleared events per path": None,
                },
                {
                    "Mechanics": mechanics,
                    "Path": "Multi-period",
                    "P(bad debt)": multi.prob_bad_debt,
                    "Positive-loss paths": int(np.count_nonzero(multi.bad_debt > 0)),
                    "Mean bad debt": multi.mean,
                    "CVaR99": multi.cvar,
                    "P(reliquidation)": multi.prob_reliquidation,
                    "Cleared events per path": multi.mean_events,
                },
            ]
        )
    return rows


def account_rows(snapshot, min_target_share: float) -> list[dict]:
    """Return target-dominant borrower records for dashboard display."""
    rows = []
    for account in snapshot.accounts:
        if (
            account.debt_usd < 10_000
            or account.collateral_usd <= 0
            or account.target_share < min_target_share
            or not 0 < account.avg_liquidation_threshold < 1
        ):
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
