"""Borrower position-book generation and health-factor accounting."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import AssetParams, PositionConfig, RiskParams


@dataclass
class PositionBook:
    """Vectorised borrower book.

    `lt` optionally carries a per-position liquidation threshold (real Aave
    accounts hold mixed collateral, so each account has its own weighted
    average LT). When None, the market-wide RiskParams threshold applies.

    `eth_debt_usd` optionally carries the portion of each position's debt
    that is ETH-denominated, valued in USD at time zero. In a scenario that
    portion scales with the ETH return, so a leveraged staking loop (LST
    collateral, WETH debt) is exposed to the LST/ETH exchange rate rather
    than the USD price level. When None, all debt is treated as USD-stable.
    """

    debt_usd: np.ndarray
    coll_units: np.ndarray
    hf0: np.ndarray
    lt: np.ndarray | None = None
    eth_debt_usd: np.ndarray | None = None

    @property
    def total_debt(self) -> float:
        return float(self.debt_usd.sum())

    def collateral_value(self, price: float) -> np.ndarray:
        return self.coll_units * price

    def health_factor(self, price: float, lt: float | None = None) -> np.ndarray:
        thresholds = self.lt if lt is None and self.lt is not None else lt
        return self.collateral_value(price) * thresholds / self.debt_usd


def scale_book(book: PositionBook, target_debt_usd: float) -> PositionBook:
    """Scale exposure to a target aggregate debt, preserving health factors."""
    if target_debt_usd < 0:
        raise ValueError("target_debt_usd must be non-negative")
    if book.total_debt <= 0:
        raise ValueError("cannot scale an empty book")
    factor = target_debt_usd / book.total_debt
    return PositionBook(
        debt_usd=book.debt_usd * factor,
        coll_units=book.coll_units * factor,
        hf0=book.hf0.copy(),
        lt=None if book.lt is None else book.lt.copy(),
        eth_debt_usd=None if book.eth_debt_usd is None else book.eth_debt_usd * factor,
    )


def build_position_book(
    pos: PositionConfig,
    risk: RiskParams,
    asset: AssetParams,
    rng: np.random.Generator,
    total_debt_usd: float | None = None,
) -> PositionBook:
    """Generate a synthetic borrower book at a target aggregate debt."""
    n = pos.n_borrowers
    target_debt = pos.total_debt_usd if total_debt_usd is None else total_debt_usd
    if n <= 0:
        raise ValueError("n_borrowers must be positive")
    if target_debt < 0:
        raise ValueError("total_debt_usd must be non-negative")

    raw = rng.pareto(pos.debt_pareto_alpha, size=n) + 1.0
    debt = raw / raw.sum() * target_debt

    log_hf = np.log(pos.hf0_median) + pos.hf0_sigma * rng.standard_normal(n)
    hf0_floor = max(pos.hf0_floor, risk.liquidation_threshold / risk.ltv)
    hf0 = np.maximum(np.exp(log_hf), hf0_floor)

    coll_value = debt * hf0 / risk.liquidation_threshold
    coll_units = coll_value / asset.spot_price

    return PositionBook(debt_usd=debt, coll_units=coll_units, hf0=hf0)
