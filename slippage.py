"""Concentrated-liquidity liquidation slippage model."""

from __future__ import annotations

import numpy as np


def calibrate_liquidity(price: float, ref_notional_usd: float, ref_slippage: float) -> float:
    """Back out active liquidity L from one reference depth/slippage point."""
    if price <= 0:
        raise ValueError("price must be positive")
    if ref_notional_usd <= 0:
        raise ValueError("ref_notional_usd must be positive")
    if not 0 < ref_slippage < 1:
        raise ValueError("ref_slippage must be in (0, 1)")
    q = ref_notional_usd / np.sqrt(price)
    return float(q * (1.0 - ref_slippage) / ref_slippage)


def slippage(notional_usd, liquidity: float, price: float):
    """Average execution shortfall for selling notional_usd of collateral."""
    notional_usd = np.asarray(notional_usd, dtype=float)
    q = notional_usd / np.sqrt(price)
    return q / (liquidity + q)


def executable_value(notional_usd, liquidity: float, price: float):
    """USD proceeds from selling notional_usd after slippage."""
    return notional_usd * (1.0 - slippage(notional_usd, liquidity, price))
