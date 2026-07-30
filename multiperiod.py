"""Multi-period liquidation simulation with book-state evolution.

The single-period engine draws one terminal shock and marks every stalled
liquidation to fire-sale value immediately. This simulator divides the
stress window into periods and evolves the book through them:

- prices follow per-step draws of the configured return law; peg and
  depth-haircut idiosyncratic terms follow random walks whose terminal
  variance matches the single-period calibration;
- each period runs ordered (bonus-priority) queue clearing against that
  period's depth; cleared repayments and seizures update the book, so
  positions restored to the V4 target health factor can be liquidated
  again if prices keep falling;
- stalled liquidations wait instead of being marked immediately; only
  positions still under water in the final period are marked to delayed
  executable value. A price recovery can therefore rescue a stalled
  position, which the single-period convention cannot represent;
- depth consumed by cleared sales carries into the next period scaled by
  `1 - replenish` (full replenishment by default).

Multi-period simulation always uses ordered clearing; the queue-average
approximation has no per-position outcome to evolve.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import ScenarioConfig
from .liquidation import ordered_clearing, size_liquidations
from .positions import PositionBook
from .slippage import calibrate_liquidity
from .stress import sample_log_returns

# Positions with less debt than this are treated as closed.
_CLOSED_DEBT_USD = 1.0


def _idio_step_sigma(
    idio_vol: float, horizon_days: float, total_days: float, n_periods: int
) -> float:
    """Per-step sigma so the walk's terminal variance matches the window.

    `idio_vol` is calibrated as a sigma over `horizon_days`; a window of
    `total_days` should reach variance idio_vol^2 * total_days /
    horizon_days, split evenly across periods.
    """
    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    return float(idio_vol * np.sqrt(total_days / horizon_days) / np.sqrt(n_periods))


@dataclass
class MultiPeriodResult:
    """Per-path outputs of a multi-period simulation."""

    bad_debt: np.ndarray
    realized_bad_debt: np.ndarray
    terminal_marks: np.ndarray
    liquidation_events: np.ndarray
    reliquidated_positions: np.ndarray
    total_debt: float
    cvar_level: float
    n_periods: int

    @property
    def mean(self) -> float:
        return float(self.bad_debt.mean())

    @property
    def prob_bad_debt(self) -> float:
        return float((self.bad_debt > 0).mean())

    @property
    def var(self) -> float:
        return float(np.quantile(self.bad_debt, self.cvar_level))

    @property
    def cvar(self) -> float:
        losses = np.sort(self.bad_debt)
        tail_mass = max(0.0, (1.0 - self.cvar_level) * losses.size)
        tail_n = max(1, int(np.ceil(tail_mass - 1e-12)))
        return float(losses[-tail_n:].mean())

    @property
    def mean_events(self) -> float:
        return float(self.liquidation_events.mean())

    @property
    def prob_reliquidation(self) -> float:
        return float((self.reliquidated_positions > 0).mean())


def simulate_multi_period(
    config: ScenarioConfig,
    book: PositionBook,
    n_periods: int = 8,
    total_days: float = 4.0,
    replenish: float = 1.0,
    n_paths: int | None = None,
    seed: int | None = None,
    chunk_size: int | None = None,
    step_log_returns: np.ndarray | None = None,
    peg_idio_steps: np.ndarray | None = None,
    haircut_idio_steps: np.ndarray | None = None,
) -> MultiPeriodResult:
    """Simulate the book through `n_periods` covering `total_days`.

    The `*_steps` arrays (shape (n_periods, n_paths)) override the random
    draws, which makes deterministic path tests possible.
    """
    if n_periods < 1:
        raise ValueError("n_periods must be at least 1")
    if not 0.0 <= replenish <= 1.0:
        raise ValueError("replenish must be in [0, 1]")

    stress = config.stress
    sim = config.sim
    n = n_paths or sim.n_scenarios
    rng = np.random.default_rng(sim.seed if seed is None else seed)
    chunk = chunk_size or sim.chunk_size

    dt_years = (total_days / n_periods) / 365.0
    if step_log_returns is None:
        step_log_returns = np.stack(
            [sample_log_returns(stress, dt_years, n, rng) for _ in range(n_periods)]
        )
    # StressConfig idiosyncratic vols are sigmas at the configured
    # single-period horizon; the walks must reach the terminal variance of
    # the actual window, so the per-step sigma carries the
    # total_days / horizon_days rescaling that the return draws get from
    # dt_years automatically.
    peg_sigma = _idio_step_sigma(
        stress.peg_idio_vol, stress.horizon_days, total_days, n_periods
    )
    haircut_sigma = _idio_step_sigma(
        stress.depth_idio_vol, stress.horizon_days, total_days, n_periods
    )
    if peg_idio_steps is None:
        peg_idio_steps = rng.normal(0.0, peg_sigma, (n_periods, n))
    if haircut_idio_steps is None:
        haircut_idio_steps = rng.normal(0.0, haircut_sigma, (n_periods, n))
    for name, arr in (
        ("step_log_returns", step_log_returns),
        ("peg_idio_steps", peg_idio_steps),
        ("haircut_idio_steps", haircut_idio_steps),
    ):
        if arr.shape != (n_periods, n):
            raise ValueError(f"{name} must have shape (n_periods, n_paths)")

    spot = config.asset.spot_price
    l0 = calibrate_liquidity(
        spot, config.liquidity.ref_notional_usd, config.liquidity.ref_slippage
    )
    depth_points = config.liquidity.depth_points
    lt_row = (
        np.full(book.debt_usd.size, config.risk.liquidation_threshold)
        if book.lt is None
        else book.lt
    )

    bad = np.empty(n)
    realized_arr = np.empty(n)
    marks_arr = np.empty(n)
    events_arr = np.empty(n)
    reliq_arr = np.empty(n)

    for start in range(0, n, chunk):
        end = min(start + chunk, n)
        out = _simulate_chunk(
            config,
            book,
            lt_row,
            step_log_returns[:, start:end],
            peg_idio_steps[:, start:end],
            haircut_idio_steps[:, start:end],
            l0,
            depth_points,
            replenish,
        )
        realized_arr[start:end], marks_arr[start:end] = out[0], out[1]
        events_arr[start:end], reliq_arr[start:end] = out[2], out[3]

    bad = realized_arr + marks_arr
    return MultiPeriodResult(
        bad_debt=bad,
        realized_bad_debt=realized_arr,
        terminal_marks=marks_arr,
        liquidation_events=events_arr,
        reliquidated_positions=reliq_arr,
        total_debt=book.total_debt,
        cvar_level=sim.cvar_level,
        n_periods=n_periods,
    )


def _simulate_chunk(
    config: ScenarioConfig,
    book: PositionBook,
    lt_row: np.ndarray,
    step_log_returns: np.ndarray,
    peg_idio_steps: np.ndarray,
    haircut_idio_steps: np.ndarray,
    l0: float,
    depth_points: list[list[float]] | None,
    replenish: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    stress = config.stress
    risk = config.risk
    n_periods, n_paths = step_log_returns.shape
    n_pos = book.debt_usd.size
    spot = config.asset.spot_price

    cum_log = np.cumsum(step_log_returns, axis=0)
    cum_ret = np.exp(cum_log) - 1.0
    peg_walk = np.cumsum(peg_idio_steps, axis=0)
    haircut_walk = np.cumsum(haircut_idio_steps, axis=0)

    # Book state. ETH-denominated debt is kept at its time-zero USD value
    # and revalued by the cumulative return each period; repayments scale
    # both parts pro-rata.
    eth0 = np.zeros(n_pos) if book.eth_debt_usd is None else book.eth_debt_usd
    stable_state = np.broadcast_to(book.debt_usd - eth0, (n_paths, n_pos)).copy()
    eth_state = np.broadcast_to(eth0, (n_paths, n_pos)).copy()
    units_state = np.broadcast_to(book.coll_units, (n_paths, n_pos)).copy()
    lt = lt_row[None, :]

    realized = np.zeros(n_paths)
    marks = np.zeros(n_paths)
    events = np.zeros((n_paths, n_pos), dtype=np.int32)
    carry_volume = np.zeros(n_paths)

    for t in range(n_periods):
        drawdown = np.maximum(0.0, -cum_ret[t])
        peg = np.clip(
            stress.base_peg_drop + stress.peg_crash_beta * drawdown + peg_walk[t],
            0.0,
            stress.max_peg_drop,
        )
        haircut = np.clip(
            stress.base_depth_haircut + stress.depth_crash_beta * drawdown + haircut_walk[t],
            0.0,
            stress.max_depth_haircut,
        )
        price = np.maximum(spot * (1.0 + cum_ret[t]) * (1.0 - peg), 1e-9)
        depth_liquidity = l0 * (1.0 - haircut)

        debt = np.maximum(stable_state + eth_state * (1.0 + cum_ret[t])[:, None], 1e-9)
        coll_value = units_state * price[:, None]
        sizing = size_liquidations(debt, coll_value, lt, risk)
        alive = debt > _CLOSED_DEBT_USD
        liquidatable = sizing.liquidatable & alive

        outcome = ordered_clearing(
            seize=np.broadcast_to(sizing.seize, coll_value.shape),
            bonus=np.broadcast_to(sizing.bonus, coll_value.shape),
            liquidatable=liquidatable,
            coll_price=price,
            depth_liquidity=depth_liquidity,
            depth_points=depth_points,
            depth_haircut=haircut,
            curve_offset=carry_volume,
        )

        cleared = outcome.cleared
        insolvent = cleared & (sizing.bad_cleared > 0.0)
        solvent = cleared & ~insolvent

        # Insolvent clears: all collateral is seized, the residual debt is
        # realized as bad debt, and the position closes.
        realized += np.where(insolvent, sizing.bad_cleared, 0.0).sum(axis=1)
        # Solvent clears: repay and seize update the book state pro-rata.
        repay_frac = np.where(solvent, sizing.repay / debt, 0.0)
        keep = 1.0 - repay_frac
        stable_state *= keep
        eth_state *= keep
        stable_state = np.where(insolvent, 0.0, stable_state)
        eth_state = np.where(insolvent, 0.0, eth_state)
        units_state = np.where(
            cleared,
            np.maximum(units_state - sizing.seize / price[:, None], 0.0),
            units_state,
        )
        events += cleared.astype(np.int32)

        stalled = liquidatable & ~cleared
        if t == n_periods - 1:
            # End of the window: whatever could not clear is marked to
            # delayed executable value at its own assessed slippage.
            mark = np.maximum(
                0.0,
                debt
                - coll_value
                * (1.0 - outcome.tranche_slippage)
                * (1.0 - stress.liquidation_delay_drawdown),
            )
            marks += np.where(stalled, mark, 0.0).sum(axis=1)

        carry_volume = (carry_volume + outcome.cleared_volume) * (1.0 - replenish)

    reliq = (events >= 2).sum(axis=1).astype(float)
    return realized, marks, events.sum(axis=1).astype(float), reliq
