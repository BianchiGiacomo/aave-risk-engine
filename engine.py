"""Single-Spoke Monte Carlo risk-budgeting engine."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from .config import RiskParams, ScenarioConfig
from .liquidation import process_chunk
from .positions import PositionBook, build_position_book, scale_book
from .stress import Scenarios, sample_scenarios


@dataclass
class RiskResult:
    """Outputs of a single Monte Carlo run.

    `liquidated_usd` is the volume that cleared within liquidator
    break-even per scenario; `queued_usd` is the full liquidatable sale
    volume submitted (the difference stalled).
    """

    bad_debt: np.ndarray
    slippage: np.ndarray
    liquidated_usd: np.ndarray
    frac_liquidated: np.ndarray
    total_debt: float
    cvar_level: float
    queued_usd: np.ndarray | None = None

    @property
    def mean(self) -> float:
        return float(self.bad_debt.mean())

    @property
    def prob_bad_debt(self) -> float:
        return float((self.bad_debt > 0).mean())

    @property
    def positive_loss_count(self) -> int:
        return int(np.count_nonzero(self.bad_debt > 0))

    @property
    def conditional_mean_bad_debt(self) -> float | None:
        positive = self.bad_debt[self.bad_debt > 0]
        return float(positive.mean()) if positive.size else None

    @property
    def cvar_tail_count(self) -> int:
        tail_mass = max(0.0, (1.0 - self.cvar_level) * self.bad_debt.size)
        return max(1, int(np.ceil(tail_mass - 1e-12)))

    def prob_bad_debt_interval(self, z: float = 1.959963984540054) -> tuple[float, float]:
        """Wilson score interval for the positive-loss probability."""
        n = self.bad_debt.size
        if n == 0:
            return float("nan"), float("nan")
        p = self.positive_loss_count / n
        z2 = z * z
        denominator = 1.0 + z2 / n
        center = (p + z2 / (2.0 * n)) / denominator
        radius = (
            z
            * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
            / denominator
        )
        low = 0.0 if self.positive_loss_count == 0 else max(0.0, center - radius)
        high = 1.0 if self.positive_loss_count == n else min(1.0, center + radius)
        return low, high

    @property
    def var(self) -> float:
        return float(np.quantile(self.bad_debt, self.cvar_level))

    @property
    def cvar(self) -> float:
        losses = np.sort(self.bad_debt)
        return float(losses[-self.cvar_tail_count :].mean())

    @property
    def worst(self) -> float:
        return float(self.bad_debt.max())

    def summary(self) -> dict:
        return {
            "total_debt": self.total_debt,
            "mean_bad_debt": self.mean,
            "prob_bad_debt": self.prob_bad_debt,
            f"VaR_{self.cvar_level:.0%}": self.var,
            f"CVaR_{self.cvar_level:.0%}": self.cvar,
            "worst_case": self.worst,
            "mean_slippage": float(self.slippage.mean()),
            "p99_slippage": float(np.quantile(self.slippage, 0.99)),
        }

    def diagnostics(self) -> dict:
        """Publication diagnostics extending the stable numeric summary."""
        probability_low, probability_high = self.prob_bad_debt_interval()
        return {
            **self.summary(),
            "prob_bad_debt_ci95": [probability_low, probability_high],
            "positive_loss_draws": self.positive_loss_count,
            "conditional_mean_bad_debt": self.conditional_mean_bad_debt,
            "cvar_tail_draws": self.cvar_tail_count,
            "tail_metrics_low_sample": self.positive_loss_count < 30,
        }


def evaluate_book(
    book: PositionBook,
    scenarios: Scenarios,
    risk: RiskParams,
    delay_drawdown: float,
    cvar_level: float,
    chunk_size: int = 2_000,
    depth_points: list[list[float]] | None = None,
) -> RiskResult:
    """Evaluate a borrower book against an externally supplied scenario set."""
    n = scenarios.coll_price.size
    bad = np.empty(n)
    slip = np.empty(n)
    liq = np.empty(n)
    queued = np.empty(n)
    frac = np.empty(n)

    for start in range(0, n, chunk_size):
        end = min(start + chunk_size, n)
        res = process_chunk(
            book,
            scenarios.coll_price[start:end],
            scenarios.depth_liquidity[start:end],
            risk,
            delay_drawdown,
            depth_points=depth_points,
            depth_haircut=scenarios.depth_haircut[start:end],
            eth_return=scenarios.eth_return[start:end],
        )
        bad[start:end] = res.bad_debt
        slip[start:end] = res.slippage
        liq[start:end] = res.liquidated_usd
        queued[start:end] = res.queued_usd
        frac[start:end] = res.frac_liquidated

    return RiskResult(
        bad_debt=bad,
        slippage=slip,
        liquidated_usd=liq,
        frac_liquidated=frac,
        total_debt=book.total_debt,
        cvar_level=cvar_level,
        queued_usd=queued,
    )


class RiskEngine:
    """Stateful single-Spoke engine with cached common-random scenarios."""

    def __init__(self, config: ScenarioConfig):
        self.config = config
        self._rng = np.random.default_rng(config.sim.seed)
        self._scenarios = sample_scenarios(
            config.stress,
            config.asset,
            config.liquidity,
            config.sim.n_scenarios,
            self._rng,
        )

    @property
    def scenarios(self) -> Scenarios:
        return self._scenarios

    def run(
        self,
        total_debt_usd: float | None = None,
        risk: RiskParams | None = None,
        book_seed: int = 0,
        book: PositionBook | None = None,
    ) -> RiskResult:
        """Evaluate a book; `book` supplies real positions instead of synthetic.

        A real book combined with `total_debt_usd` is scaled proportionally,
        preserving its health-factor distribution.
        """
        cfg = self.config
        risk = risk or cfg.risk
        if book is not None:
            if total_debt_usd is not None:
                book = scale_book(book, total_debt_usd)
        else:
            book = build_position_book(
                cfg.positions,
                risk,
                cfg.asset,
                np.random.default_rng(book_seed),
                total_debt_usd=total_debt_usd,
            )
        return self._evaluate(book, risk)

    def _evaluate(self, book: PositionBook, risk: RiskParams) -> RiskResult:
        cfg = self.config
        return evaluate_book(
            book,
            self._scenarios,
            risk,
            cfg.stress.liquidation_delay_drawdown,
            cfg.sim.cvar_level,
            chunk_size=cfg.sim.chunk_size,
            depth_points=cfg.liquidity.depth_points,
        )

    def cap_sweep(
        self,
        caps_usd: np.ndarray,
        book_seed: int = 0,
        book: PositionBook | None = None,
        risk: RiskParams | None = None,
    ) -> dict:
        caps_usd = np.asarray(caps_usd, dtype=float)
        mean = np.empty_like(caps_usd)
        cvar = np.empty_like(caps_usd)
        var = np.empty_like(caps_usd)
        prob = np.empty_like(caps_usd)
        p99s = np.empty_like(caps_usd)
        for i, cap in enumerate(caps_usd):
            r = self.run(total_debt_usd=float(cap), book_seed=book_seed, book=book, risk=risk)
            mean[i], cvar[i], var[i], prob[i] = r.mean, r.cvar, r.var, r.prob_bad_debt
            p99s[i] = np.quantile(r.slippage, 0.99)
        return {
            "caps": caps_usd,
            "mean": mean,
            "cvar": cvar,
            "var": var,
            "prob": prob,
            "p99_slippage": p99s,
        }

    def recommend_cap(
        self,
        budget_usd: float,
        cap_min: float = 1e7,
        cap_max: float | None = None,
        n_grid: int = 40,
        book_seed: int = 0,
        book: PositionBook | None = None,
        risk: RiskParams | None = None,
    ) -> dict:
        if cap_max is None:
            cap_max = self.config.positions.total_debt_usd * 2.0
        caps = np.linspace(cap_min, cap_max, n_grid)
        sweep = self.cap_sweep(caps, book_seed=book_seed, book=book, risk=risk)
        return {
            "sweep": sweep,
            "budget": budget_usd,
            "recommended_cap": _largest_cap_within_budget(caps, sweep["cvar"], budget_usd),
        }

    def ltv_sweep(self, ltvs: np.ndarray, book_seed: int = 0) -> dict:
        ltvs = np.asarray(ltvs, dtype=float)
        gap = self.config.risk.liquidation_threshold - self.config.risk.ltv
        mean = np.empty_like(ltvs)
        cvar = np.empty_like(ltvs)
        prob = np.empty_like(ltvs)
        for i, lt in enumerate(ltvs):
            risk = replace(
                self.config.risk,
                liquidation_threshold=float(lt),
                ltv=max(1e-6, float(lt) - gap),
            )
            r = self.run(risk=risk, book_seed=book_seed)
            mean[i], cvar[i], prob[i] = r.mean, r.cvar, r.prob_bad_debt
        return {"ltvs": ltvs, "mean": mean, "cvar": cvar, "prob": prob}


def _largest_cap_within_budget(caps: np.ndarray, cvar: np.ndarray, budget: float) -> float:
    within = cvar <= budget
    if not within.any():
        return float("nan")
    if within.all():
        return float(caps[-1])
    idx = np.where(within)[0].max()
    c0, c1 = caps[idx], caps[idx + 1]
    v0, v1 = cvar[idx], cvar[idx + 1]
    if v1 == v0:
        return float(c1)
    return float(c0 + (budget - v0) * (c1 - c0) / (v1 - v0))
