"""Per-scenario liquidation accounting and bad-debt computation.

The building blocks are shared by the single-period Monte Carlo engine
(`process_chunk`) and the multi-period simulator: `size_liquidations`
applies V3 or V4 sizing and bonus rules, and `ordered_clearing` runs the
sequential bonus-priority queue against the depth curve.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import RiskParams
from .positions import PositionBook
from .slippage import empirical_slippage


@dataclass
class ChunkResult:
    """Per-scenario outputs for one scenario chunk.

    `queued_usd` is the total seize value of all liquidatable positions,
    the sale volume submitted to the market. `liquidated_usd` is the
    volume that actually cleared within liquidator break-even; the
    remainder stalled. In the aggregate queue model `slippage` is the
    average over the queued volume; in ordered mode it is the realized
    average over the cleared volume.
    """

    bad_debt: np.ndarray
    liquidated_usd: np.ndarray
    queued_usd: np.ndarray
    slippage: np.ndarray
    frac_liquidated: np.ndarray


@dataclass
class LiquidationSizing:
    """Per-position sizing of one liquidation round (matrices)."""

    liquidatable: np.ndarray
    bonus: np.ndarray
    repay: np.ndarray
    seize: np.ndarray
    bad_cleared: np.ndarray
    hf: np.ndarray


@dataclass
class QueueOutcome:
    """Per-position outcome of one ordered clearing round.

    `tranche_slippage` is the marginal slippage each liquidatable position
    was assessed at; `cleared` marks positions whose tranche stayed within
    their liquidator break-even. `cleared_volume` and `proceeds` are per
    scenario and exclude any `curve_offset` carried into the round.
    """

    cleared: np.ndarray
    tranche_slippage: np.ndarray
    cleared_volume: np.ndarray
    proceeds: np.ndarray


def size_liquidations(
    debt: np.ndarray,
    coll_value: np.ndarray,
    lt,
    risk: RiskParams,
) -> LiquidationSizing:
    """Apply V3 or V4 bonus and repayment sizing to the current book state."""
    hf = coll_value * lt / debt
    liquidatable = hf < 1.0

    if risk.v4 is not None:
        v4 = risk.v4
        # Dynamic bonus: liquidation_bonus_factor * max_bonus just under
        # par, rising linearly to the full max_bonus at or below
        # hf_max_bonus (healthFactorForMaxBonus).
        depth_into_default = np.clip((1.0 - hf) / (1.0 - v4.hf_max_bonus), 0.0, 1.0)
        bonus = v4.max_bonus * (
            v4.liquidation_bonus_factor
            + (1.0 - v4.liquidation_bonus_factor) * depth_into_default
        )
        # Repay sized to restore the target health factor:
        # (C - R(1+b)) * LT = target * (D - R). A non-positive denominator
        # means no partial repayment can reach the target, so liquidate all.
        target = v4.target_health_factor
        denom = target - (1.0 + bonus) * lt
        to_target = np.where(
            denom > 1e-9,
            (target * debt - coll_value * lt) / np.maximum(denom, 1e-9),
            debt,
        )
        repay_sized = np.maximum(to_target, v4.close_factor_floor * debt)
        repay = np.minimum(np.minimum(repay_sized, debt), coll_value / (1.0 + bonus))
        if v4.dust_threshold_usd > 0:
            # Positions that would be left with dust are closed in full
            # (still capped by seizable collateral).
            repay = np.where(
                debt - repay < v4.dust_threshold_usd,
                np.minimum(debt, coll_value / (1.0 + bonus)),
                repay,
            )
        repay = np.where(liquidatable, repay, 0.0)
    else:
        bonus = np.asarray(risk.liquidation_bonus)
        close = np.where(hf < risk.full_liquidation_hf, 1.0, risk.close_factor)
        repay = np.where(
            liquidatable,
            np.minimum(debt * close, coll_value / (1.0 + bonus)),
            0.0,
        )
    seize = repay * (1.0 + bonus)
    bad_cleared = np.maximum(0.0, debt - coll_value / (1.0 + bonus))
    return LiquidationSizing(
        liquidatable=liquidatable,
        bonus=bonus,
        repay=repay,
        seize=seize,
        bad_cleared=bad_cleared,
        hf=hf,
    )


def _proceeds(
    volume_usd: np.ndarray,
    coll_price: np.ndarray,
    depth_liquidity: np.ndarray,
    depth_points: list[list[float]] | None,
    depth_haircut: np.ndarray | None,
) -> np.ndarray:
    """USD received for selling `volume_usd` from the top of the curve."""
    if depth_points is not None and depth_haircut is not None:
        effective = volume_usd / np.maximum(1e-9, 1.0 - depth_haircut)
        return volume_usd * (1.0 - empirical_slippage(effective, depth_points))
    q = volume_usd / np.sqrt(coll_price)
    return volume_usd * (1.0 - q / (depth_liquidity + q))


def ordered_clearing(
    seize: np.ndarray,
    bonus: np.ndarray,
    liquidatable: np.ndarray,
    coll_price: np.ndarray,
    depth_liquidity: np.ndarray,
    depth_points: list[list[float]] | None,
    depth_haircut: np.ndarray | None,
    curve_offset: np.ndarray | None = None,
) -> QueueOutcome:
    """Sequential queue clearing in bonus-priority order.

    Per scenario, liquidatable positions are processed in descending bonus
    order (seize size breaking ties, mirroring liquidator profit priority).
    Each tranche is assessed at its marginal slippage on the cumulative
    proceeds curve: cleared tranches consume depth, stalled tranches do
    not. `curve_offset` starts the walk deeper into the curve, carrying
    depth already consumed earlier (multi-period use). Vectorized across
    scenarios; the Python loop runs over queue rank only.
    """
    n_scen, n_pos = seize.shape
    rows = np.arange(n_scen)

    key = np.where(liquidatable, bonus, -np.inf)
    order = np.lexsort((-seize, -key), axis=1)

    offset = np.zeros(n_scen) if curve_offset is None else curve_offset
    offset_proceeds = _proceeds(
        offset, coll_price, depth_liquidity, depth_points, depth_haircut
    )

    cleared = np.zeros((n_scen, n_pos), dtype=bool)
    tranche_slippage = np.zeros((n_scen, n_pos))
    cleared_volume = np.zeros(n_scen)
    curve_volume = offset.copy()
    curve_proceeds = offset_proceeds.copy()

    for rank in range(n_pos):
        idx = order[:, rank]
        active = liquidatable[rows, idx]
        if not active.any():
            continue
        q_k = np.where(active, seize[rows, idx], 0.0)
        b_k = bonus[rows, idx]

        after = _proceeds(
            curve_volume + q_k, coll_price, depth_liquidity, depth_points, depth_haircut
        )
        s_k = np.where(q_k > 0, 1.0 - (after - curve_proceeds) / np.maximum(q_k, 1e-12), 0.0)
        clears = active & (s_k <= b_k / (1.0 + b_k))

        tranche_slippage[rows, idx] = np.where(active, s_k, 0.0)
        cleared[rows, idx] = clears
        cleared_volume = np.where(clears, cleared_volume + q_k, cleared_volume)
        curve_volume = np.where(clears, curve_volume + q_k, curve_volume)
        curve_proceeds = np.where(clears, after, curve_proceeds)

    return QueueOutcome(
        cleared=cleared,
        tranche_slippage=tranche_slippage,
        cleared_volume=cleared_volume,
        proceeds=curve_proceeds - offset_proceeds,
    )


def process_chunk(
    book: PositionBook,
    coll_price: np.ndarray,
    depth_liquidity: np.ndarray,
    risk: RiskParams,
    delay_drawdown: float,
    depth_points: list[list[float]] | None = None,
    depth_haircut: np.ndarray | None = None,
    eth_return: np.ndarray | None = None,
) -> ChunkResult:
    """Compute liquidation outcomes for a chunk of scenarios.

    Slippage is a liquidator cost while liquidations clear. It becomes a
    protocol recovery cost only when the queue stalls because the slippage is
    above liquidator break-even: bonus / (1 + bonus).

    With `depth_points` (and the scenario `depth_haircut`), slippage comes
    from interpolated real quotes evaluated at notional / (1 - haircut)
    instead of the analytic curve.

    With `eth_return` and a book carrying `eth_debt_usd`, the ETH-denominated
    portion of each debt scales with the scenario ETH return, so correlated
    loops are stressed by the collateral's exchange rate and peg terms, not
    by the USD price level.
    """
    if book.eth_debt_usd is not None and eth_return is not None:
        stable = (book.debt_usd - book.eth_debt_usd)[None, :]
        floating = book.eth_debt_usd[None, :] * (1.0 + eth_return[:, None])
        debt = np.maximum(stable + floating, 1e-9)
    else:
        debt = book.debt_usd[None, :]
    units = book.coll_units[None, :]
    price = coll_price[:, None]

    lt = risk.liquidation_threshold if book.lt is None else book.lt[None, :]
    coll_value = units * price
    sizing = size_liquidations(debt, coll_value, lt, risk)
    liquidatable, bonus, seize = sizing.liquidatable, sizing.bonus, sizing.seize
    bad_cleared = sizing.bad_cleared
    queued_usd = seize.sum(axis=1)

    if risk.ordered_queue:
        shape = coll_value.shape
        outcome = ordered_clearing(
            seize=np.broadcast_to(seize, shape),
            bonus=np.broadcast_to(bonus, shape),
            liquidatable=np.broadcast_to(liquidatable, shape),
            coll_price=coll_price,
            depth_liquidity=depth_liquidity,
            depth_points=depth_points,
            depth_haircut=depth_haircut,
        )
        stalled = np.broadcast_to(liquidatable, shape) & ~outcome.cleared
        bad_stalled = np.maximum(
            0.0,
            debt - coll_value * (1.0 - outcome.tranche_slippage) * (1.0 - delay_drawdown),
        )
        bad = np.where(outcome.cleared, bad_cleared, np.where(stalled, bad_stalled, 0.0))
        bad_debt = bad.sum(axis=1)
        liquidated_usd = outcome.cleared_volume
        s = np.where(
            outcome.cleared_volume > 0,
            1.0 - outcome.proceeds / np.maximum(outcome.cleared_volume, 1e-12),
            0.0,
        )
    else:
        if depth_points is not None and depth_haircut is not None:
            effective = queued_usd / np.maximum(1e-9, 1.0 - depth_haircut)
            s = np.where(queued_usd > 0, empirical_slippage(effective, depth_points), 0.0)
        else:
            q = queued_usd / np.sqrt(coll_price)
            s = np.where(queued_usd > 0, q / (depth_liquidity + q), 0.0)

        # A position's liquidation stalls when the queue-average slippage
        # exceeds its own liquidator break-even. Under V3 the bonus is one
        # scalar, so the whole queue stalls together; under V4 positions
        # deeper into default carry larger bonuses and keep clearing at
        # slippage levels that stall near-par liquidations.
        stalled_pos = s[:, None] > bonus / (1.0 + bonus)

        recovery_stalled = coll_value * ((1.0 - s) * (1.0 - delay_drawdown))[:, None]
        bad_stalled = np.maximum(0.0, debt - recovery_stalled)
        bad = np.where(
            liquidatable,
            np.where(stalled_pos, bad_stalled, bad_cleared),
            0.0,
        )
        bad_debt = bad.sum(axis=1)
        liquidated_usd = np.where(liquidatable & ~stalled_pos, seize, 0.0).sum(axis=1)

    total_debt = book.total_debt
    frac_liquidated = (
        np.where(liquidatable, debt, 0.0).sum(axis=1) / total_debt
        if total_debt > 0
        else np.zeros_like(coll_price)
    )

    return ChunkResult(
        bad_debt=bad_debt,
        liquidated_usd=liquidated_usd,
        queued_usd=queued_usd,
        slippage=s,
        frac_liquidated=frac_liquidated,
    )
