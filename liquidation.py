"""Per-scenario liquidation accounting and bad-debt computation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import RiskParams
from .positions import PositionBook
from .slippage import empirical_slippage


@dataclass
class ChunkResult:
    """Per-scenario outputs for one scenario chunk."""

    bad_debt: np.ndarray
    liquidated_usd: np.ndarray
    slippage: np.ndarray
    frac_liquidated: np.ndarray


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
        bonus = risk.liquidation_bonus
        close = np.where(hf < risk.full_liquidation_hf, 1.0, risk.close_factor)
        repay = np.where(
            liquidatable,
            np.minimum(debt * close, coll_value / (1.0 + bonus)),
            0.0,
        )
    seize = repay * (1.0 + bonus)
    liquidated_usd = seize.sum(axis=1)

    if depth_points is not None and depth_haircut is not None:
        effective = liquidated_usd / np.maximum(1e-9, 1.0 - depth_haircut)
        s = np.where(liquidated_usd > 0, empirical_slippage(effective, depth_points), 0.0)
    else:
        q = liquidated_usd / np.sqrt(coll_price)
        s = np.where(liquidated_usd > 0, q / (depth_liquidity + q), 0.0)

    # A position's liquidation stalls when slippage exceeds its own
    # liquidator break-even. Under V3 the bonus is one scalar, so the whole
    # queue stalls together; under V4 positions deeper into default carry
    # larger bonuses and keep clearing at slippage levels that stall
    # near-par liquidations.
    stalled_pos = s[:, None] > bonus / (1.0 + bonus)

    bad_cleared = np.maximum(0.0, debt - coll_value / (1.0 + bonus))
    recovery_stalled = coll_value * ((1.0 - s) * (1.0 - delay_drawdown))[:, None]
    bad_stalled = np.maximum(0.0, debt - recovery_stalled)

    bad = np.where(
        liquidatable,
        np.where(stalled_pos, bad_stalled, bad_cleared),
        0.0,
    )
    bad_debt = bad.sum(axis=1)

    total_debt = book.total_debt
    frac_liquidated = (
        np.where(liquidatable, debt, 0.0).sum(axis=1) / total_debt
        if total_debt > 0
        else np.zeros_like(coll_price)
    )

    return ChunkResult(
        bad_debt=bad_debt,
        liquidated_usd=liquidated_usd,
        slippage=s,
        frac_liquidated=frac_liquidated,
    )
