"""ARFC largest-borrower liquidation clearance test.

The Aave Risk Framework (governance ARFC, June 2026) requires secondary
market depth to "clear the largest expected borrower within the asset's
liquidation bonus at acceptable slippage". In this engine that is exactly
the liquidator break-even condition s(Q) <= bonus / (1 + bonus), evaluated
at the notional a liquidation of the largest borrower would sell.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..slippage import calibrate_liquidity, empirical_slippage, max_notional_within, slippage
from .snapshot import MarketSnapshot


@dataclass
class ClearanceResult:
    """Outcome of the clearance test under quiet and stressed depth."""

    largest_account: str
    largest_borrower_usd: float
    largest_debt_usd: float
    largest_target_collateral_usd: float
    largest_eth_debt_share: float
    top5_borrowers_usd: float
    breakeven_slippage: float
    slippage_quiet: float
    slippage_stressed: float
    slippage_quiet_is_lower_bound: bool
    slippage_stressed_is_lower_bound: bool
    max_quoted_usd: float
    max_clearable_usd_quiet: float
    max_clearable_usd_stressed: float
    passes_quiet: bool
    passes_stressed: bool
    depth_haircut_stressed: float


def max_clearable_notional(liquidity: float, price: float, bonus: float) -> float:
    """Largest sale that still satisfies s(Q) <= bonus / (1 + bonus).

    With s = q / (L + q), the break-even b = bonus / (1 + bonus) is hit at
    q* = L * b / (1 - b), i.e. Q* = sqrt(P) * L * bonus (the algebra
    collapses because b / (1 - b) = bonus).
    """
    return float(np.sqrt(price) * liquidity * bonus)


def arfc_clearance_test(
    snapshot: MarketSnapshot,
    stressed_haircut: float = 0.5,
    min_target_share: float = 0.5,
) -> ClearanceResult:
    """Can snapshot depth clear the largest borrower within the bonus?

    The sale notional per account is the seized collateral,
    min(collateral, debt * (1 + bonus)), that is, full liquidation, which is
    the conservative reading of "largest expected borrower".

    Accounts are included regardless of debt denomination: a WETH-debt
    looper's collateral still hits this asset's depth curve when liquidated,
    so clearance is a pure market-depth question. This intentionally differs
    from the USD-shock book, which excludes loopers because the price shock
    does not apply to them.
    """
    reserve = snapshot.reserve
    bonus = reserve.liquidation_bonus
    price = reserve.price_usd
    if snapshot.depth is None:
        raise ValueError("snapshot has no depth calibration")

    accounts = [
        a
        for a in snapshot.accounts
        if a.debt_usd > 0 and a.collateral_usd > 0 and a.target_share >= min_target_share
    ]
    if not accounts:
        raise ValueError("no accounts pass the target-share filter")

    # Only the target-asset collateral is sold on this asset's depth curve.
    sales = sorted(
        (
            (min(a.target_collateral_usd, a.debt_usd * (1.0 + bonus)), a)
            for a in accounts
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    largest, largest_account = sales[0]
    top5 = float(sum(notional for notional, _ in sales[:5]))
    breakeven = bonus / (1.0 + bonus)

    points = snapshot.depth.points
    if points and len(points) >= 2:
        # Interpolate observed quotes; a stress haircut h scales sale size
        # by 1 / (1 - h), which matches the analytic curve's behaviour.
        s_quiet = float(empirical_slippage(largest, points))
        s_stressed = float(empirical_slippage(largest / (1.0 - stressed_haircut), points))
        clearable_quiet = max_notional_within(points, breakeven)
        clearable_stressed = clearable_quiet * (1.0 - stressed_haircut)
        max_quoted = max(float(point[0]) for point in points)
        quiet_is_lower_bound = largest > max_quoted
        stressed_is_lower_bound = largest / (1.0 - stressed_haircut) > max_quoted
    else:
        liquidity = calibrate_liquidity(
            price, snapshot.depth.ref_notional_usd, snapshot.depth.ref_slippage
        )
        l_stressed = liquidity * (1.0 - stressed_haircut)
        s_quiet = float(slippage(largest, liquidity, price))
        s_stressed = float(slippage(largest, l_stressed, price))
        clearable_quiet = max_clearable_notional(liquidity, price, bonus)
        clearable_stressed = max_clearable_notional(l_stressed, price, bonus)
        max_quoted = float("nan")
        quiet_is_lower_bound = False
        stressed_is_lower_bound = False

    return ClearanceResult(
        largest_account=largest_account.address,
        largest_borrower_usd=largest,
        largest_debt_usd=largest_account.debt_usd,
        largest_target_collateral_usd=largest_account.target_collateral_usd,
        largest_eth_debt_share=largest_account.eth_debt_share,
        top5_borrowers_usd=top5,
        breakeven_slippage=breakeven,
        slippage_quiet=s_quiet,
        slippage_stressed=s_stressed,
        slippage_quiet_is_lower_bound=quiet_is_lower_bound,
        slippage_stressed_is_lower_bound=stressed_is_lower_bound,
        max_quoted_usd=max_quoted,
        max_clearable_usd_quiet=clearable_quiet,
        max_clearable_usd_stressed=clearable_stressed,
        passes_quiet=largest <= clearable_quiet,
        passes_stressed=largest <= clearable_stressed,
        depth_haircut_stressed=stressed_haircut,
    )
