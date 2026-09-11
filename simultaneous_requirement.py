"""Test 2: how much debt must be repaid at once under a stated stress path.

For each scenario of the stress path, the positions whose health factor
falls below one are sized for liquidation with the protocol's own rules,
using the same sizing function and the same ETH-denominated debt
treatment as the Monte Carlo engine. The simultaneous requirement is
reported in two units that must not be mixed:

  repayment   debt a liquidator must repay at the moment of liquidation,
              which is what has to be financed;
  seizure     collateral received in exchange, which is what has to be
              sold or held.

This is a first-round requirement. Under the V3 close factor a position
between the full-liquidation threshold and one is half repaid, and a
second round inside the horizon is not counted here; the multi-period
simulator covers re-liquidation.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import RiskParams
from .liquidation import size_liquidations
from .positions import PositionBook
from .stress import Scenarios


@dataclass(frozen=True)
class Distribution:
    """Quantiles of one per-scenario quantity, plus its tail mean."""

    p50: float
    p90: float
    p95: float
    p99: float
    maximum: float
    tail_mean: float

    @classmethod
    def of(cls, values: np.ndarray, tail_index: np.ndarray) -> "Distribution":
        q = np.quantile(values, [0.50, 0.90, 0.95, 0.99])
        return cls(
            p50=float(q[0]),
            p90=float(q[1]),
            p95=float(q[2]),
            p99=float(q[3]),
            maximum=float(values.max()),
            tail_mean=float(values[tail_index].mean()),
        )


@dataclass(frozen=True)
class SimultaneityResult:
    n_scenarios: int
    tail_level: float
    tail_scenarios: int
    accounts: int
    book_debt_usd: float
    prob_any_liquidation: float
    repayment_usd: Distribution
    seizure_usd: Distribution
    liquidatable_positions: Distribution
    largest_share_of_tail_repayment: float
    largest_single_repayment_in_tail_usd: float


def compute(
    book: PositionBook,
    scenarios: Scenarios,
    risk: RiskParams,
    tail_level: float = 0.99,
    chunk_size: int = 2_000,
) -> SimultaneityResult:
    """Size every liquidation in every scenario and summarise the totals.

    The tail is the worst (1 - tail_level) share of scenarios ranked by
    total repayment, matching the engine's CVaR convention.
    """

    if not 0.0 < tail_level < 1.0:
        raise ValueError("tail level must be in (0, 1)")
    n = scenarios.coll_price.size
    repay_total = np.empty(n)
    seize_total = np.empty(n)
    count = np.empty(n)
    largest = np.empty(n)

    lt_row = risk.liquidation_threshold if book.lt is None else book.lt[None, :]
    for start in range(0, n, chunk_size):
        end = min(start + chunk_size, n)
        price = scenarios.coll_price[start:end][:, None]
        # Same debt construction as liquidation.process_chunk: the
        # ETH-denominated share of each debt moves with the ETH return.
        if book.eth_debt_usd is not None:
            stable = (book.debt_usd - book.eth_debt_usd)[None, :]
            floating = book.eth_debt_usd[None, :] * (
                1.0 + scenarios.eth_return[start:end][:, None]
            )
            debt = np.maximum(stable + floating, 1e-9)
        else:
            debt = np.broadcast_to(book.debt_usd[None, :], (end - start, book.debt_usd.size))
        coll_value = book.coll_units[None, :] * price
        sizing = size_liquidations(debt, coll_value, lt_row, risk)
        repay = sizing.repay
        repay_total[start:end] = repay.sum(axis=1)
        seize_total[start:end] = sizing.seize.sum(axis=1)
        count[start:end] = sizing.liquidatable.sum(axis=1)
        largest[start:end] = repay.max(axis=1)

    # Same tail count as RiskResult.cvar_tail_count, including its guard
    # against floating-point overshoot in (1 - level) * n.
    k = max(1, int(np.ceil(max(0.0, (1.0 - tail_level) * n) - 1e-12)))
    tail_index = np.argsort(repay_total)[-k:]
    tail_repay = repay_total[tail_index]
    with np.errstate(invalid="ignore", divide="ignore"):
        shares = np.where(tail_repay > 0, largest[tail_index] / tail_repay, 0.0)

    return SimultaneityResult(
        n_scenarios=n,
        tail_level=tail_level,
        tail_scenarios=k,
        accounts=int(book.debt_usd.size),
        book_debt_usd=float(book.total_debt),
        prob_any_liquidation=float((count > 0).mean()),
        repayment_usd=Distribution.of(repay_total, tail_index),
        seizure_usd=Distribution.of(seize_total, tail_index),
        liquidatable_positions=Distribution.of(count, tail_index),
        largest_share_of_tail_repayment=float(shares.mean()),
        largest_single_repayment_in_tail_usd=float(largest[tail_index].max()),
    )
