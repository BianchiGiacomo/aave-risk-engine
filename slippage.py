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


def empirical_slippage(notional_usd, points):
    """Slippage interpolated from observed (notional_usd, slippage) quotes.

    Real routed depth can fall off a cliff once concentrated pools are
    exhausted, which no single-L curve represents; this interpolates the
    observed points instead. Linear in notional below the first point
    (slippage is asymptotically linear in size), linear in log-notional
    between points, and flat beyond the last point -- which understates
    losses out there, so quote ladders should extend past the sizes that
    matter.

    A depth haircut h is applied by evaluating at notional / (1 - h):
    for the analytic curve s(q) = q / (L(1-h) + q) this identity is exact.
    """
    pts = sorted((float(n), float(s)) for n, s in points if n > 0)
    if len(pts) < 2:
        raise ValueError("need at least two depth points")
    n_arr = np.array([p[0] for p in pts])
    s_arr = np.maximum.accumulate(np.array([p[1] for p in pts]))

    notional_usd = np.asarray(notional_usd, dtype=float)
    below = notional_usd < n_arr[0]
    interp = np.interp(np.log(np.maximum(notional_usd, n_arr[0])), np.log(n_arr), s_arr)
    small = s_arr[0] * notional_usd / n_arr[0]
    return np.clip(np.where(below, small, interp), 0.0, 0.995)


def max_notional_within(points, max_slippage: float) -> float:
    """Largest sale keeping empirical slippage at or below max_slippage."""
    pts = sorted((float(n), float(s)) for n, s in points if n > 0)
    n_arr = np.array([p[0] for p in pts])
    s_arr = np.maximum.accumulate(np.array([p[1] for p in pts]))
    if max_slippage >= s_arr[-1]:
        return float(n_arr[-1])
    if max_slippage <= s_arr[0]:
        return float(n_arr[0] * max_slippage / s_arr[0]) if s_arr[0] > 0 else float(n_arr[0])
    # Invert the monotone piecewise curve in log-notional space.
    keep = np.concatenate(([True], np.diff(s_arr) > 0))
    return float(np.exp(np.interp(max_slippage, s_arr[keep], np.log(n_arr[keep]))))
