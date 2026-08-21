"""Cash-flow accounting for a liquidator that warehouses seized collateral."""

from __future__ import annotations

from dataclasses import dataclass, replace

from scipy.optimize import minimize_scalar

from .time_to_exit import (
    ExitAssumptions,
    dex_capacity_at,
    redemption_capacity_at,
    time_to_clear_hours as combined_time_to_clear_hours,
)


HOURS_PER_YEAR = 365.0 * 24.0


@dataclass(frozen=True)
class LiquidatorAssumptions:
    """Costs and residual risks for a delta-hedged warehouse strategy.

    `canonical_loss` is an impairment between the liquidation valuation and
    final canonical recovery after hedging ETH/USD, so it affects both DEX and
    primary-redemption exits. `dex_market_discount` is a secondary-market
    discount that affects only collateral sold through the DEX.
    """

    funding_annual_rate: float = 0.10
    hurdle_annual_rate: float = 0.10
    hedge_entry_cost: float = 0.001
    hedge_carry_annual_rate: float = 0.02
    dex_execution_loss: float = 0.01
    redemption_loss: float = 0.0
    canonical_loss: float = 0.0
    dex_market_discount: float = 0.0
    route_strategy: str = "profit_maximizing"
    fixed_cost_usd: float = 0.0
    step_hours: float = 1.0
    max_horizon_hours: float = 365.0 * 24.0

    def __post_init__(self) -> None:
        rates = (
            self.funding_annual_rate,
            self.hurdle_annual_rate,
            self.hedge_entry_cost,
            self.hedge_carry_annual_rate,
            self.dex_execution_loss,
            self.redemption_loss,
            self.canonical_loss,
            self.dex_market_discount,
        )
        if any(value < 0.0 for value in rates):
            raise ValueError("cost and loss rates must be non-negative")
        if any(
            value >= 1.0
            for value in (
                self.dex_execution_loss,
                self.redemption_loss,
                self.canonical_loss,
                self.dex_market_discount,
            )
        ):
            raise ValueError("execution, redemption, and recovery losses must be below 1")
        if self.fixed_cost_usd < 0.0:
            raise ValueError("fixed cost must be non-negative")
        if self.route_strategy not in {"capacity_first", "profit_maximizing"}:
            raise ValueError(
                "route strategy must be capacity_first or profit_maximizing"
            )
        if self.step_hours <= 0.0:
            raise ValueError("step hours must be positive")
        if self.max_horizon_hours <= 0.0:
            raise ValueError("maximum horizon must be positive")


@dataclass(frozen=True)
class LiquidatorCashFlow:
    """Incremental exit and cumulative balance at one simulation time."""

    horizon_hours: float
    dex_exit_usd: float
    redemption_exit_usd: float
    cumulative_exit_usd: float
    unresolved_collateral_usd: float
    cash_recovery_usd: float
    cash_balance_usd: float
    capital_outstanding_usd: float
    cumulative_funding_cost_usd: float
    cumulative_hedge_carry_usd: float


@dataclass(frozen=True)
class LiquidatorResult:
    """Warehouse-strategy balance sheet through complete or timed-out exit."""

    debt_repaid_usd: float
    seized_collateral_usd: float
    liquidation_bonus: float
    route_strategy: str
    route_optimization_evaluations: int
    exit_assumptions: ExitAssumptions
    assumptions: LiquidatorAssumptions
    cash_flows: tuple[LiquidatorCashFlow, ...]
    cleared: bool
    time_to_clear_hours: float | None
    weighted_average_exit_hours: float | None
    dex_exit_usd: float
    redemption_exit_usd: float
    unresolved_collateral_usd: float
    realized_recovery_usd: float
    gross_bonus_usd: float
    funding_cost_usd: float
    hedge_entry_cost_usd: float
    hedge_carry_cost_usd: float
    fixed_cost_usd: float
    peak_capital_usd: float
    capital_days_usd: float
    accounting_profit_usd: float | None
    hurdle_charge_usd: float
    economic_profit_usd: float | None
    accounting_roi: float | None
    economic_roi: float | None
    economic_clearance_pass: bool


def _recovery_factors(assumptions: LiquidatorAssumptions) -> dict[str, float]:
    canonical_recovery = 1.0 - assumptions.canonical_loss
    return {
        "dex": canonical_recovery
        * (1.0 - assumptions.dex_market_discount)
        * (1.0 - assumptions.dex_execution_loss),
        "redemption": canonical_recovery
        * (1.0 - assumptions.redemption_loss),
    }


def _dex_time_for_capacity(
    exit_assumptions: ExitAssumptions, target_usd: float
) -> float | None:
    if target_usd <= exit_assumptions.instant_dex_capacity_usd:
        return 0.0
    instant = exit_assumptions.instant_dex_capacity_usd
    refill = exit_assumptions.dex_refill_hours
    if instant <= 0.0 or refill is None:
        return None
    return (target_usd / instant - 1.0) * refill


def _redemption_time_for_capacity(
    exit_assumptions: ExitAssumptions, target_usd: float
) -> float | None:
    if target_usd <= 0.0:
        return 0.0
    throughput = exit_assumptions.redemption_capacity_usd_per_day
    if throughput <= 0.0:
        return None
    return (
        exit_assumptions.redemption_delay_hours
        + 24.0 * target_usd / throughput
    )


def _simulate_route_targets(
    debt_repaid_usd: float,
    seized_collateral_usd: float,
    exit_assumptions: ExitAssumptions,
    assumptions: LiquidatorAssumptions,
    dex_target_usd: float,
    redemption_target_usd: float,
) -> LiquidatorResult:
    """Simulate earliest execution for one fixed DEX/redemption allocation."""
    if debt_repaid_usd <= 0.0:
        raise ValueError("debt repaid must be positive")
    if seized_collateral_usd <= 0.0:
        raise ValueError("seized collateral must be positive")
    if dex_target_usd < 0.0 or redemption_target_usd < 0.0:
        raise ValueError("route targets must be non-negative")
    target_total = dex_target_usd + redemption_target_usd
    tolerance = max(1e-6, seized_collateral_usd * 1e-12)
    if target_total > seized_collateral_usd + tolerance:
        raise ValueError("route targets cannot exceed seized collateral")

    bonus = seized_collateral_usd / debt_repaid_usd - 1.0
    factors = _recovery_factors(assumptions)
    hedge_entry = seized_collateral_usd * assumptions.hedge_entry_cost
    cash = -(debt_repaid_usd + hedge_entry + assumptions.fixed_cost_usd)
    peak_capital = max(0.0, -cash)
    capital_hours = 0.0
    funding_cost = 0.0
    hedge_carry = 0.0
    realized_recovery = 0.0
    dex_exited = 0.0
    redemption_exited = 0.0
    weighted_exit_hours = 0.0
    remaining = seized_collateral_usd
    cash_flows: list[LiquidatorCashFlow] = []

    def apply_exit(
        horizon: float, dex_capacity: float, redemption_capacity: float
    ) -> tuple[float, float, float]:
        nonlocal cash, remaining, realized_recovery
        nonlocal dex_exited, redemption_exited, weighted_exit_hours
        available = {
            "dex": max(0.0, dex_capacity),
            "redemption": max(0.0, redemption_capacity),
        }
        used = {"dex": 0.0, "redemption": 0.0}
        recovery = 0.0
        for route in ("dex", "redemption"):
            amount = min(remaining, available[route])
            if amount <= 0.0:
                continue
            used[route] = amount
            remaining -= amount
            route_recovery = amount * factors[route]
            recovery += route_recovery
            weighted_exit_hours += amount * horizon
        cash += recovery
        realized_recovery += recovery
        dex_exited += used["dex"]
        redemption_exited += used["redemption"]
        return used["dex"], used["redemption"], recovery

    initial_dex = min(
        dex_target_usd,
        dex_capacity_at(exit_assumptions, 0.0),
    )
    initial_redemption = min(
        redemption_target_usd,
        redemption_capacity_at(exit_assumptions, 0.0),
    )
    dex_used, redemption_used, recovery = apply_exit(
        0.0, initial_dex, initial_redemption
    )
    cash_flows.append(
        LiquidatorCashFlow(
            horizon_hours=0.0,
            dex_exit_usd=dex_used,
            redemption_exit_usd=redemption_used,
            cumulative_exit_usd=seized_collateral_usd - remaining,
            unresolved_collateral_usd=remaining,
            cash_recovery_usd=recovery,
            cash_balance_usd=cash,
            capital_outstanding_usd=max(0.0, -cash),
            cumulative_funding_cost_usd=funding_cost,
            cumulative_hedge_carry_usd=hedge_carry,
        )
    )

    current = 0.0
    target_times = tuple(
        value
        for value in (
            _dex_time_for_capacity(exit_assumptions, dex_target_usd),
            _redemption_time_for_capacity(
                exit_assumptions, redemption_target_usd
            ),
        )
        if value is not None and value > 0.0
    )
    while remaining > tolerance and current < assumptions.max_horizon_hours:
        proposed = min(
            current + assumptions.step_hours, assumptions.max_horizon_hours
        )
        future_events = [
            value for value in target_times if current < value < proposed
        ]
        if future_events:
            proposed = min(future_events)
        interval = proposed - current
        dex_increment = min(
            dex_target_usd - dex_exited,
            max(
                0.0,
                dex_capacity_at(exit_assumptions, proposed) - dex_exited,
            ),
        )
        redemption_increment = min(
            redemption_target_usd - redemption_exited,
            max(
                0.0,
                redemption_capacity_at(exit_assumptions, proposed)
                - redemption_exited,
            ),
        )
        dex_increment = max(0.0, dex_increment)
        redemption_increment = max(0.0, redemption_increment)

        outstanding = max(0.0, -cash)
        capital_hours += outstanding * interval
        incremental_funding = (
            outstanding
            * assumptions.funding_annual_rate
            * interval
            / HOURS_PER_YEAR
        )
        incremental_hedge_carry = (
            remaining
            * assumptions.hedge_carry_annual_rate
            * interval
            / HOURS_PER_YEAR
        )
        funding_cost += incremental_funding
        hedge_carry += incremental_hedge_carry
        cash -= incremental_funding + incremental_hedge_carry
        peak_capital = max(peak_capital, max(0.0, -cash))

        dex_used, redemption_used, recovery = apply_exit(
            proposed, dex_increment, redemption_increment
        )
        peak_capital = max(peak_capital, max(0.0, -cash))
        cash_flows.append(
            LiquidatorCashFlow(
                horizon_hours=proposed,
                dex_exit_usd=dex_used,
                redemption_exit_usd=redemption_used,
                cumulative_exit_usd=seized_collateral_usd - remaining,
                unresolved_collateral_usd=remaining,
                cash_recovery_usd=recovery,
                cash_balance_usd=cash,
                capital_outstanding_usd=max(0.0, -cash),
                cumulative_funding_cost_usd=funding_cost,
                cumulative_hedge_carry_usd=hedge_carry,
            )
        )
        current = proposed

    cleared = remaining <= tolerance
    time_to_clear = current if cleared else None
    average_exit = (
        weighted_exit_hours / seized_collateral_usd if cleared else None
    )
    capital_days = capital_hours / 24.0
    hurdle_charge = capital_days * assumptions.hurdle_annual_rate / 365.0
    accounting_profit = cash if cleared else None
    economic_profit = cash - hurdle_charge if cleared else None
    accounting_roi = (
        accounting_profit / peak_capital
        if accounting_profit is not None and peak_capital > 0.0
        else None
    )
    economic_roi = (
        economic_profit / peak_capital
        if economic_profit is not None and peak_capital > 0.0
        else None
    )

    return LiquidatorResult(
        debt_repaid_usd=debt_repaid_usd,
        seized_collateral_usd=seized_collateral_usd,
        liquidation_bonus=bonus,
        route_strategy="fixed_allocation",
        route_optimization_evaluations=1,
        exit_assumptions=exit_assumptions,
        assumptions=assumptions,
        cash_flows=tuple(cash_flows),
        cleared=cleared,
        time_to_clear_hours=time_to_clear,
        weighted_average_exit_hours=average_exit,
        dex_exit_usd=dex_exited,
        redemption_exit_usd=redemption_exited,
        unresolved_collateral_usd=max(0.0, remaining),
        realized_recovery_usd=realized_recovery,
        gross_bonus_usd=seized_collateral_usd - debt_repaid_usd,
        funding_cost_usd=funding_cost,
        hedge_entry_cost_usd=hedge_entry,
        hedge_carry_cost_usd=hedge_carry,
        fixed_cost_usd=assumptions.fixed_cost_usd,
        peak_capital_usd=peak_capital,
        capital_days_usd=capital_days,
        accounting_profit_usd=accounting_profit,
        hurdle_charge_usd=hurdle_charge,
        economic_profit_usd=economic_profit,
        accounting_roi=accounting_roi,
        economic_roi=economic_roi,
        economic_clearance_pass=bool(cleared and economic_profit is not None and economic_profit >= 0.0),
    )


def _capacity_first_targets(
    seized_collateral_usd: float,
    exit_assumptions: ExitAssumptions,
    max_horizon_hours: float,
) -> tuple[float, float]:
    clear_time = combined_time_to_clear_hours(
        seized_collateral_usd, exit_assumptions
    )
    horizon = (
        clear_time
        if clear_time is not None and clear_time <= max_horizon_hours
        else max_horizon_hours
    )
    dex_target = min(
        seized_collateral_usd,
        dex_capacity_at(exit_assumptions, horizon),
    )
    redemption_target = min(
        seized_collateral_usd - dex_target,
        redemption_capacity_at(exit_assumptions, horizon),
    )
    return dex_target, redemption_target


def _profit_maximizing_result(
    debt_repaid_usd: float,
    seized_collateral_usd: float,
    exit_assumptions: ExitAssumptions,
    assumptions: LiquidatorAssumptions,
) -> LiquidatorResult:
    horizon = assumptions.max_horizon_hours
    dex_limit = min(
        seized_collateral_usd,
        dex_capacity_at(exit_assumptions, horizon),
    )
    redemption_limit = min(
        seized_collateral_usd,
        redemption_capacity_at(exit_assumptions, horizon),
    )
    tolerance = max(1e-6, seized_collateral_usd * 1e-12)
    if dex_limit + redemption_limit < seized_collateral_usd - tolerance:
        dex_target, redemption_target = _capacity_first_targets(
            seized_collateral_usd,
            exit_assumptions,
            horizon,
        )
        result = _simulate_route_targets(
            debt_repaid_usd,
            seized_collateral_usd,
            exit_assumptions,
            assumptions,
            dex_target,
            redemption_target,
        )
        return replace(
            result,
            route_strategy="profit_maximizing",
            route_optimization_evaluations=1,
        )

    lower = max(0.0, seized_collateral_usd - redemption_limit)
    upper = min(seized_collateral_usd, dex_limit)
    if upper - lower <= tolerance:
        result = _simulate_route_targets(
            debt_repaid_usd,
            seized_collateral_usd,
            exit_assumptions,
            assumptions,
            upper,
            seized_collateral_usd - upper,
        )
        return replace(
            result,
            route_strategy="profit_maximizing",
            route_optimization_evaluations=1,
        )

    evaluated: dict[float, LiquidatorResult] = {}

    def evaluate(dex_target: float) -> LiquidatorResult:
        bounded = min(upper, max(lower, float(dex_target)))
        key = round(bounded, 8)
        if key not in evaluated:
            evaluated[key] = _simulate_route_targets(
                debt_repaid_usd,
                seized_collateral_usd,
                exit_assumptions,
                assumptions,
                bounded,
                seized_collateral_usd - bounded,
            )
        return evaluated[key]

    def objective(dex_target: float) -> float:
        profit = evaluate(dex_target).economic_profit_usd
        return 1e100 if profit is None else -profit

    grid_size = 13
    grid = [
        lower + (upper - lower) * index / (grid_size - 1)
        for index in range(grid_size)
    ]
    grid_results = [evaluate(value) for value in grid]
    best_index = max(
        range(grid_size),
        key=lambda index: (
            -1e100
            if grid_results[index].economic_profit_usd is None
            else grid_results[index].economic_profit_usd
        ),
    )
    left = grid[max(0, best_index - 1)]
    right = grid[min(grid_size - 1, best_index + 1)]
    if right - left > tolerance:
        optimum = minimize_scalar(
            objective,
            bounds=(left, right),
            method="bounded",
            options={
                "xatol": max(1.0, seized_collateral_usd * 1e-7),
                "maxiter": 60,
            },
        )
        evaluate(float(optimum.x))

    def result_key(result: LiquidatorResult) -> tuple[float, float]:
        profit = (
            -1e100
            if result.economic_profit_usd is None
            else result.economic_profit_usd
        )
        clear_time = (
            1e100
            if result.time_to_clear_hours is None
            else result.time_to_clear_hours
        )
        return profit, -clear_time

    best = max(evaluated.values(), key=result_key)
    return replace(
        best,
        route_strategy="profit_maximizing",
        route_optimization_evaluations=len(evaluated),
    )


def simulate_liquidator_balance_sheet(
    debt_repaid_usd: float,
    seized_collateral_usd: float,
    exit_assumptions: ExitAssumptions,
    assumptions: LiquidatorAssumptions = LiquidatorAssumptions(),
) -> LiquidatorResult:
    """Simulate a full-upfront warehouse under the selected route strategy.

    Capacity-first exits through both routes as soon as capacity appears. The
    profit-maximizing strategy chooses the total DEX versus redemption split
    that maximizes economic profit, then executes each assigned route at its
    earliest available capacity. The optimization is one-dimensional and does
    not assume collateral can be assigned to both routes.
    """
    if assumptions.route_strategy == "profit_maximizing":
        return _profit_maximizing_result(
            debt_repaid_usd,
            seized_collateral_usd,
            exit_assumptions,
            assumptions,
        )
    dex_target, redemption_target = _capacity_first_targets(
        seized_collateral_usd,
        exit_assumptions,
        assumptions.max_horizon_hours,
    )
    result = _simulate_route_targets(
        debt_repaid_usd,
        seized_collateral_usd,
        exit_assumptions,
        assumptions,
        dex_target,
        redemption_target,
    )
    return replace(
        result,
        route_strategy="capacity_first",
        route_optimization_evaluations=1,
    )


def _break_even_loss(
    debt_repaid_usd: float,
    seized_collateral_usd: float,
    exit_assumptions: ExitAssumptions,
    assumptions: LiquidatorAssumptions,
    field: str,
    tolerance: float = 1e-7,
) -> float | None:
    """Largest selected loss parameter with non-negative economic profit."""

    def profit(value: float) -> float | None:
        result = simulate_liquidator_balance_sheet(
            debt_repaid_usd,
            seized_collateral_usd,
            exit_assumptions,
            replace(assumptions, **{field: value}),
        )
        return result.economic_profit_usd

    low = 0.0
    high = 0.995
    low_profit = profit(low)
    high_profit = profit(high)
    if low_profit is None or low_profit < 0.0:
        return None
    if high_profit is not None and high_profit >= 0.0:
        return high
    for _ in range(60):
        mid = (low + high) / 2.0
        mid_profit = profit(mid)
        if mid_profit is not None and mid_profit >= 0.0:
            low = mid
        else:
            high = mid
        if high - low <= tolerance:
            break
    return low


def break_even_canonical_loss(
    debt_repaid_usd: float,
    seized_collateral_usd: float,
    exit_assumptions: ExitAssumptions,
    assumptions: LiquidatorAssumptions,
    tolerance: float = 1e-7,
) -> float | None:
    """Largest canonical impairment with non-negative economic profit."""
    return _break_even_loss(
        debt_repaid_usd,
        seized_collateral_usd,
        exit_assumptions,
        assumptions,
        "canonical_loss",
        tolerance,
    )


def break_even_dex_market_discount(
    debt_repaid_usd: float,
    seized_collateral_usd: float,
    exit_assumptions: ExitAssumptions,
    assumptions: LiquidatorAssumptions,
    tolerance: float = 1e-7,
) -> float | None:
    """Largest DEX-only market discount with non-negative economic profit."""
    return _break_even_loss(
        debt_repaid_usd,
        seized_collateral_usd,
        exit_assumptions,
        assumptions,
        "dex_market_discount",
        tolerance,
    )


def minimum_liquidation_bonus(
    debt_repaid_usd: float,
    exit_assumptions: ExitAssumptions,
    assumptions: LiquidatorAssumptions,
    max_bonus: float = 1.0,
    tolerance: float = 1e-7,
) -> float | None:
    """Minimum bonus producing non-negative economic profit."""
    if debt_repaid_usd <= 0.0:
        raise ValueError("debt repaid must be positive")
    if max_bonus <= 0.0:
        raise ValueError("maximum bonus must be positive")

    def profit(bonus: float) -> float | None:
        result = simulate_liquidator_balance_sheet(
            debt_repaid_usd,
            debt_repaid_usd * (1.0 + bonus),
            exit_assumptions,
            assumptions,
        )
        return result.economic_profit_usd

    low = 0.0
    low_profit = profit(low)
    if low_profit is not None and low_profit >= 0.0:
        return 0.0

    high = None
    for index in range(1, 201):
        candidate = max_bonus * index / 200.0
        candidate_profit = profit(candidate)
        if candidate_profit is None:
            break
        if candidate_profit >= 0.0:
            high = candidate
            break
        low = candidate
    if high is None:
        return None

    for _ in range(60):
        mid = (low + high) / 2.0
        mid_profit = profit(mid)
        if mid_profit is not None and mid_profit >= 0.0:
            high = mid
        else:
            low = mid
        if high - low <= tolerance:
            break
    return high
