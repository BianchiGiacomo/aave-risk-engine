"""Drawdown and warehouse sensitivities for NAV-priced RWA collateral."""

from __future__ import annotations

import csv
import datetime as dt
from collections import deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PriceObservation:
    date: dt.date
    adjusted_close_usd: float


@dataclass(frozen=True)
class ReturnWindow:
    start_date: dt.date
    end_date: dt.date
    sessions: int
    calendar_days: int
    return_value: float


@dataclass(frozen=True)
class ConditionalWindow:
    threshold: float
    lookback_sessions: int
    prior_drawdown: float
    window: ReturnWindow


def load_adjusted_close_csv(path: str | Path) -> tuple[PriceObservation, ...]:
    """Load a strictly increasing, positive adjusted-close series."""

    observations: list[PriceObservation] = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"date", "adjusted_close_usd"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("CSV must contain date and adjusted_close_usd columns")
        for row in reader:
            date = dt.date.fromisoformat(row["date"])
            value = float(row["adjusted_close_usd"])
            if value <= 0.0:
                raise ValueError("adjusted closes must be positive")
            if observations and date <= observations[-1].date:
                raise ValueError("observation dates must be strictly increasing")
            observations.append(PriceObservation(date, value))
    if len(observations) < 2:
        raise ValueError("at least two observations are required")
    return tuple(observations)


def return_windows(
    observations: tuple[PriceObservation, ...], sessions: int
) -> tuple[ReturnWindow, ...]:
    """Compute close-to-close returns over `sessions` intervals.

    A four-session return therefore uses five dated observations.
    """

    if sessions <= 0:
        raise ValueError("sessions must be positive")
    if len(observations) <= sessions:
        raise ValueError("series is shorter than the requested window")
    windows = []
    for index in range(len(observations) - sessions):
        start = observations[index]
        end = observations[index + sessions]
        windows.append(
            ReturnWindow(
                start_date=start.date,
                end_date=end.date,
                sessions=sessions,
                calendar_days=(end.date - start.date).days,
                return_value=end.adjusted_close_usd / start.adjusted_close_usd - 1.0,
            )
        )
    return tuple(windows)


def worst_return_window(
    observations: tuple[PriceObservation, ...], sessions: int
) -> ReturnWindow:
    return min(return_windows(observations, sessions), key=lambda item: item.return_value)


def worst_forward_after_drawdown(
    observations: tuple[PriceObservation, ...],
    sessions: int,
    threshold: float,
    lookback_sessions: int,
) -> ConditionalWindow:
    """Worst forward return whose start is already below a rolling peak."""

    if not 0.0 < threshold < 1.0:
        raise ValueError("drawdown threshold must be in (0, 1)")
    if lookback_sessions <= 1:
        raise ValueError("lookback must exceed one session")
    if len(observations) < lookback_sessions + sessions:
        raise ValueError("series is shorter than lookback plus forward window")

    values = [item.adjusted_close_usd for item in observations]
    rolling_maxima = [0.0] * len(values)
    candidates: deque[int] = deque()
    for index, value in enumerate(values):
        while candidates and values[candidates[-1]] <= value:
            candidates.pop()
        candidates.append(index)
        first_valid = index - lookback_sessions + 1
        while candidates[0] < first_valid:
            candidates.popleft()
        if index >= lookback_sessions - 1:
            rolling_maxima[index] = values[candidates[0]]

    eligible: list[ConditionalWindow] = []
    final_start = len(observations) - sessions
    for index in range(lookback_sessions - 1, final_start):
        start = observations[index]
        rolling_peak = rolling_maxima[index]
        drawdown = start.adjusted_close_usd / rolling_peak - 1.0
        if drawdown > -threshold:
            continue
        end = observations[index + sessions]
        eligible.append(
            ConditionalWindow(
                threshold=threshold,
                lookback_sessions=lookback_sessions,
                prior_drawdown=drawdown,
                window=ReturnWindow(
                    start_date=start.date,
                    end_date=end.date,
                    sessions=sessions,
                    calendar_days=(end.date - start.date).days,
                    return_value=(
                        end.adjusted_close_usd / start.adjusted_close_usd - 1.0
                    ),
                ),
            )
        )
    if not eligible:
        raise ValueError("no observation satisfies the drawdown condition")
    return min(eligible, key=lambda item: item.window.return_value)


def conditional_lookback_sweep(
    observations: tuple[PriceObservation, ...],
    sessions: int,
    threshold: float,
    min_lookback: int,
    max_lookback: int,
) -> tuple[ConditionalWindow, ...]:
    if min_lookback > max_lookback:
        raise ValueError("minimum lookback cannot exceed maximum lookback")
    return tuple(
        worst_forward_after_drawdown(
            observations,
            sessions,
            threshold,
            lookback,
        )
        for lookback in range(min_lookback, max_lookback + 1)
    )


def monthly_return_windows(
    observations: tuple[PriceObservation, ...],
) -> tuple[ReturnWindow, ...]:
    """Compute calendar-month returns between consecutive month-end observations."""

    month_ends: list[PriceObservation] = []
    for observation in observations:
        key = (observation.date.year, observation.date.month)
        if not month_ends or key != (
            month_ends[-1].date.year,
            month_ends[-1].date.month,
        ):
            month_ends.append(observation)
        else:
            month_ends[-1] = observation
    return tuple(
        ReturnWindow(
            start_date=start.date,
            end_date=end.date,
            sessions=0,
            calendar_days=(end.date - start.date).days,
            return_value=end.adjusted_close_usd / start.adjusted_close_usd - 1.0,
        )
        for start, end in zip(month_ends, month_ends[1:])
    )


def minimum_economic_bonus(
    recovery_loss: float,
    calendar_days: float,
    funding_annual_rate: float,
    hurdle_annual_rate: float,
) -> float:
    """Closed-form bonus for a lump recovery after a fixed warehouse horizon."""

    if not 0.0 <= recovery_loss < 1.0:
        raise ValueError("recovery loss must be in [0, 1)")
    if calendar_days < 0.0:
        raise ValueError("calendar days must be non-negative")
    if funding_annual_rate < 0.0 or hurdle_annual_rate < 0.0:
        raise ValueError("annual rates must be non-negative")
    capital_charge = (
        funding_annual_rate + hurdle_annual_rate
    ) * calendar_days / 365.0
    return (1.0 + capital_charge) / (1.0 - recovery_loss) - 1.0


def maximum_supported_recovery_loss(
    economic_compensation_rate: float,
    calendar_days: float,
    funding_annual_rate: float,
    hurdle_annual_rate: float,
) -> float:
    """Invert the lump-recovery bonus equation into a loss ceiling."""

    if economic_compensation_rate < 0.0:
        raise ValueError("economic compensation rate must be non-negative")
    if calendar_days < 0.0:
        raise ValueError("calendar days must be non-negative")
    if funding_annual_rate < 0.0 or hurdle_annual_rate < 0.0:
        raise ValueError("annual rates must be non-negative")
    capital_charge = (
        funding_annual_rate + hurdle_annual_rate
    ) * calendar_days / 365.0
    if economic_compensation_rate < capital_charge:
        raise ValueError("compensation does not cover the zero-loss capital charge")
    return 1.0 - (1.0 + capital_charge) / (1.0 + economic_compensation_rate)


def stress_shape_bracket(
    proxy_window_loss: float,
    proxy_worst_month_loss: float,
    target_worst_month_loss: float,
) -> tuple[float, float]:
    """Transfer the proxy's within-month stress concentration to a target."""

    values = (proxy_window_loss, proxy_worst_month_loss, target_worst_month_loss)
    if any(value <= 0.0 for value in values):
        raise ValueError("stress losses must be positive")
    concentration_ratio = proxy_window_loss / proxy_worst_month_loss
    bracket = target_worst_month_loss * concentration_ratio
    if bracket >= 1.0:
        raise ValueError("stress bracket must remain below 100%")
    return concentration_ratio, bracket
