"""Deterministic liquidation-capacity curves over explicit exit horizons."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


DEFAULT_HORIZONS_HOURS = (0.0, 1.0, 6.0, 24.0, 72.0, 168.0)


@dataclass(frozen=True)
class ExitAssumptions:
    """Capacity and delayed-recovery assumptions for one exit regime.

    `dex_refill_hours` is the time needed to add one more equivalent instant
    capacity. `None` disables DEX replenishment. Redemption capacity starts
    only after `redemption_delay_hours` and then accumulates linearly.
    """

    instant_dex_capacity_usd: float
    dex_refill_hours: float | None
    redemption_capacity_usd_per_day: float = 0.0
    redemption_delay_hours: float = 24.0
    stalled_drawdown: float = 0.10

    def __post_init__(self) -> None:
        if self.instant_dex_capacity_usd < 0.0:
            raise ValueError("instant DEX capacity must be non-negative")
        if self.dex_refill_hours is not None and self.dex_refill_hours <= 0.0:
            raise ValueError("DEX refill hours must be positive or None")
        if self.redemption_capacity_usd_per_day < 0.0:
            raise ValueError("redemption capacity must be non-negative")
        if self.redemption_delay_hours < 0.0:
            raise ValueError("redemption delay must be non-negative")
        if not 0.0 <= self.stalled_drawdown <= 1.0:
            raise ValueError("stalled drawdown must be in [0, 1]")


@dataclass(frozen=True)
class ExitPoint:
    """Cumulative capacity and unresolved exposure at one horizon."""

    horizon_hours: float
    dex_capacity_usd: float
    redemption_capacity_usd: float
    total_capacity_usd: float
    cleared_sale_usd: float
    unresolved_sale_usd: float
    unresolved_debt_at_risk_usd: float
    conditional_bad_debt_usd: float
    passes: bool


@dataclass(frozen=True)
class ExitCurve:
    """Time-to-exit result for one set of assumptions."""

    sale_usd: float
    liquidation_bonus: float
    assumptions: ExitAssumptions
    points: tuple[ExitPoint, ...]
    time_to_clear_hours: float | None


def dex_capacity_at(assumptions: ExitAssumptions, horizon_hours: float) -> float:
    """Cumulative DEX capacity, including the initial instant batch."""
    if horizon_hours < 0.0:
        raise ValueError("horizon must be non-negative")
    instant = assumptions.instant_dex_capacity_usd
    if assumptions.dex_refill_hours is None:
        return instant
    return instant * (1.0 + horizon_hours / assumptions.dex_refill_hours)


def redemption_capacity_at(
    assumptions: ExitAssumptions, horizon_hours: float
) -> float:
    """Cumulative primary-redemption capacity available by a horizon."""
    if horizon_hours < 0.0:
        raise ValueError("horizon must be non-negative")
    active_hours = max(0.0, horizon_hours - assumptions.redemption_delay_hours)
    return assumptions.redemption_capacity_usd_per_day * active_hours / 24.0


def conditional_stalled_loss(
    unresolved_sale_usd: float,
    liquidation_bonus: float,
    stalled_drawdown: float,
) -> tuple[float, float]:
    """Debt at risk and conditional loss on an unresolved liquidation tranche.

    A sale notional Q corresponds to Q / (1 + bonus) of repayable debt. If the
    unresolved collateral is marked down by `stalled_drawdown`, the conditional
    loss is the shortfall between that debt and delayed collateral recovery.
    This is a tranche-level stress mark, not a forecast of whole-account loss.
    """
    if unresolved_sale_usd < 0.0:
        raise ValueError("unresolved sale must be non-negative")
    if liquidation_bonus < 0.0:
        raise ValueError("liquidation bonus must be non-negative")
    if not 0.0 <= stalled_drawdown <= 1.0:
        raise ValueError("stalled drawdown must be in [0, 1]")
    debt_at_risk = unresolved_sale_usd / (1.0 + liquidation_bonus)
    delayed_recovery = unresolved_sale_usd * (1.0 - stalled_drawdown)
    return debt_at_risk, max(0.0, debt_at_risk - delayed_recovery)


def time_to_clear_hours(sale_usd: float, assumptions: ExitAssumptions) -> float | None:
    """Analytic first time cumulative DEX plus redemption capacity clears sale."""
    if sale_usd < 0.0:
        raise ValueError("sale must be non-negative")
    instant = assumptions.instant_dex_capacity_usd
    if sale_usd <= instant:
        return 0.0

    dex_rate = (
        0.0
        if assumptions.dex_refill_hours is None
        else instant / assumptions.dex_refill_hours
    )
    redemption_rate = assumptions.redemption_capacity_usd_per_day / 24.0
    delay = assumptions.redemption_delay_hours
    gap = sale_usd - instant

    if dex_rate > 0.0:
        dex_only_time = gap / dex_rate
        if redemption_rate == 0.0 or dex_only_time <= delay:
            return dex_only_time

    capacity_at_delay = instant + dex_rate * delay
    if sale_usd <= capacity_at_delay:
        return delay
    combined_rate = dex_rate + redemption_rate
    if combined_rate <= 0.0:
        return None
    return delay + (sale_usd - capacity_at_delay) / combined_rate


def required_redemption_usd_per_day(
    sale_usd: float,
    instant_dex_capacity_usd: float,
    dex_refill_hours: float | None,
    horizon_hours: float,
    redemption_delay_hours: float,
) -> float | None:
    """Redemption throughput needed to clear by the selected horizon.

    Returns `None` when a positive gap remains but redemption has not started.
    """
    base = ExitAssumptions(
        instant_dex_capacity_usd=instant_dex_capacity_usd,
        dex_refill_hours=dex_refill_hours,
        redemption_delay_hours=redemption_delay_hours,
    )
    gap = max(0.0, sale_usd - dex_capacity_at(base, horizon_hours))
    if gap == 0.0:
        return 0.0
    active_days = (horizon_hours - redemption_delay_hours) / 24.0
    if active_days <= 0.0:
        return None
    return gap / active_days


def build_exit_curve(
    sale_usd: float,
    liquidation_bonus: float,
    assumptions: ExitAssumptions,
    horizons_hours: Iterable[float] = DEFAULT_HORIZONS_HOURS,
) -> ExitCurve:
    """Evaluate cumulative capacity and conditional unresolved loss by horizon."""
    if sale_usd < 0.0:
        raise ValueError("sale must be non-negative")
    if liquidation_bonus < 0.0:
        raise ValueError("liquidation bonus must be non-negative")
    horizons = tuple(sorted(set(float(value) for value in horizons_hours)))
    if not horizons or horizons[0] < 0.0:
        raise ValueError("horizons must contain non-negative values")

    points = []
    for horizon in horizons:
        dex = dex_capacity_at(assumptions, horizon)
        redemption = redemption_capacity_at(assumptions, horizon)
        total = dex + redemption
        cleared = min(sale_usd, total)
        unresolved = max(0.0, sale_usd - cleared)
        debt_at_risk, conditional_loss = conditional_stalled_loss(
            unresolved, liquidation_bonus, assumptions.stalled_drawdown
        )
        points.append(
            ExitPoint(
                horizon_hours=horizon,
                dex_capacity_usd=dex,
                redemption_capacity_usd=redemption,
                total_capacity_usd=total,
                cleared_sale_usd=cleared,
                unresolved_sale_usd=unresolved,
                unresolved_debt_at_risk_usd=debt_at_risk,
                conditional_bad_debt_usd=conditional_loss,
                passes=total + 1e-9 >= sale_usd,
            )
        )
    return ExitCurve(
        sale_usd=sale_usd,
        liquidation_bonus=liquidation_bonus,
        assumptions=assumptions,
        points=tuple(points),
        time_to_clear_hours=time_to_clear_hours(sale_usd, assumptions),
    )
