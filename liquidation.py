"""Per-scenario liquidation accounting and bad-debt computation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import RiskParams
from .positions import PositionBook


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
) -> ChunkResult:
    """Compute liquidation outcomes for a chunk of scenarios.

    Slippage is a liquidator cost while liquidations clear. It becomes a
    protocol recovery cost only when the queue stalls because the slippage is
    above liquidator break-even: bonus / (1 + bonus).
    """
    debt = book.debt_usd[None, :]
    units = book.coll_units[None, :]
    price = coll_price[:, None]

    coll_value = units * price
    hf = coll_value * risk.liquidation_threshold / debt
    liquidatable = hf < 1.0

    close = np.where(hf < risk.full_liquidation_hf, 1.0, risk.close_factor)
    repay = np.where(
        liquidatable,
        np.minimum(debt * close, coll_value / (1.0 + risk.liquidation_bonus)),
        0.0,
    )
    seize = repay * (1.0 + risk.liquidation_bonus)
    liquidated_usd = seize.sum(axis=1)

    q = liquidated_usd / np.sqrt(coll_price)
    s = np.where(liquidated_usd > 0, q / (depth_liquidity + q), 0.0)

    stall_threshold = risk.liquidation_bonus / (1.0 + risk.liquidation_bonus)
    stalled = s > stall_threshold

    bad_cleared = np.maximum(
        0.0,
        debt - coll_value / (1.0 + risk.liquidation_bonus),
    )
    recovery_stalled = coll_value * ((1.0 - s) * (1.0 - delay_drawdown))[:, None]
    bad_stalled = np.maximum(0.0, debt - recovery_stalled)

    bad = np.where(
        liquidatable,
        np.where(stalled[:, None], bad_stalled, bad_cleared),
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
