"""Borrower position-book generation and health-factor accounting."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import AssetParams, PositionConfig, RiskParams


@dataclass
class PositionBook:
    """Vectorised borrower book."""

    debt_usd: np.ndarray
    coll_units: np.ndarray
    hf0: np.ndarray

    @property
    def total_debt(self) -> float:
        return float(self.debt_usd.sum())

    def collateral_value(self, price: float) -> np.ndarray:
        return self.coll_units * price

    def health_factor(self, price: float, lt: float) -> np.ndarray:
        return self.collateral_value(price) * lt / self.debt_usd


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
