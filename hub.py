"""Multi-Spoke Hub liquidity allocation for Aave V4 style markets."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import ScenarioConfig, SimConfig, StressConfig
from .engine import evaluate_book
from .positions import build_position_book
from .stress import Scenarios, sample_log_returns, sample_scenarios


def _cvar(losses: np.ndarray, level: float) -> float:
    s = np.sort(losses)
    tail_n = max(1, int(np.ceil((1.0 - level) * s.size - 1e-12)))
    return float(s[-tail_n:].mean())


@dataclass
class Spoke:
    """One collateral market drawing credit from the Hub."""

    name: str
    config: ScenarioConfig
    rho: float = 1.0


@dataclass
class HubResult:
    """Outcome of a Hub allocation."""

    allocation: dict[str, float]
    hub_cvar: float
    standalone_cvar: dict[str, float]
    marginal_cvar: dict[str, float]
    total_allocated: float
    budget: float
    diversification_benefit: float

    def table(self) -> list[dict]:
        return [
            {
                "spoke": name,
                "credit_line": self.allocation[name],
                "standalone_cvar": self.standalone_cvar[name],
                "marginal_cvar_per_$": self.marginal_cvar[name],
            }
            for name in self.allocation
        ]


class Hub:
    """Shared-liquidity Hub feeding several Spokes."""

    def __init__(
        self,
        spokes: list[Spoke],
        market_stress: StressConfig,
        hub_balance: float,
        sim: SimConfig | None = None,
        severity: float = 1.0,
    ):
        if not spokes:
            raise ValueError("Hub requires at least one Spoke")
        if hub_balance <= 0:
            raise ValueError("hub_balance must be positive")
        if severity <= 0:
            raise ValueError("severity must be positive")
        self.spokes = spokes
        self.market_stress = market_stress
        self.hub_balance = hub_balance
        self.sim = sim or SimConfig()
        self.severity = severity
        self.h = market_stress.horizon_days / 365.0

    def systemic_factor(self) -> np.ndarray:
        """Unit-variance systemic factor carrying the return law's tail shape."""
        rng = np.random.default_rng(self.sim.seed)
        m = sample_log_returns(self.market_stress, self.h, self.sim.n_scenarios, rng)
        std = m.std()
        if std <= 0:
            raise ValueError("systemic factor has zero variance")
        return (m - m.mean()) / std

    def _spoke_scenarios(self, spoke: Spoke, z: np.ndarray, idx: int) -> Scenarios:
        n = self.sim.n_scenarios
        rng = np.random.default_rng(self.sim.seed + 1 + idx)
        st = spoke.config.stress
        sigma = st.eth_annual_vol * self.severity * np.sqrt(self.h)
        rho = float(np.clip(spoke.rho, 0.0, 1.0))
        u = rho * z + np.sqrt(1.0 - rho**2) * rng.standard_normal(n)
        log_ret = st.eth_annual_drift * self.h - 0.5 * sigma**2 + sigma * u
        return sample_scenarios(
            st,
            spoke.config.asset,
            spoke.config.liquidity,
            n,
            rng,
            log_return=log_ret,
        )

    def _scenario_set(self, z: np.ndarray) -> dict[str, Scenarios]:
        return {
            sp.name: self._spoke_scenarios(sp, z, i)
            for i, sp in enumerate(self.spokes)
        }

    def _spoke_bad_debt(
        self,
        spoke: Spoke,
        scen: Scenarios,
        credit: float,
        book_seed: int = 0,
    ) -> np.ndarray:
        book = build_position_book(
            spoke.config.positions,
            spoke.config.risk,
            spoke.config.asset,
            np.random.default_rng(book_seed),
            total_debt_usd=credit,
        )
        res = evaluate_book(
            book,
            scen,
            spoke.config.risk,
            spoke.config.stress.liquidation_delay_drawdown,
            self.sim.cvar_level,
            chunk_size=self.sim.chunk_size,
        )
        return res.bad_debt

    def evaluate(self, allocation: dict[str, float], book_seed: int = 0) -> HubResult:
        z = self.systemic_factor()
        scen = self._scenario_set(z)
        bad = {
            sp.name: self._spoke_bad_debt(sp, scen[sp.name], allocation[sp.name], book_seed)
            for sp in self.spokes
        }
        return self._summarise(allocation, scen, bad, budget=float("nan"), book_seed=book_seed)

    def allocate(
        self,
        budget_usd: float,
        increment_usd: float | None = None,
        max_total_usd: float | None = None,
        book_seed: int = 0,
    ) -> HubResult:
        """Greedily allocate credit to the lowest marginal Hub-CVaR Spoke."""
        n = self.sim.n_scenarios
        inc = increment_usd or self.hub_balance / 40.0
        cap_total = max_total_usd or self.hub_balance

        z = self.systemic_factor()
        scen = self._scenario_set(z)
        credit = {sp.name: 0.0 for sp in self.spokes}
        bad = {sp.name: np.zeros(n) for sp in self.spokes}
        total = 0.0

        while total + inc <= cap_total + 1e-9:
            best: tuple[str, np.ndarray, float] | None = None
            for sp in self.spokes:
                trial = self._spoke_bad_debt(
                    sp, scen[sp.name], credit[sp.name] + inc, book_seed
                )
                hub_bad = trial + sum(bad[o.name] for o in self.spokes if o.name != sp.name)
                cv = _cvar(hub_bad, self.sim.cvar_level)
                if best is None or cv < best[2]:
                    best = (sp.name, trial, cv)
            assert best is not None
            if np.isfinite(budget_usd) and best[2] > budget_usd:
                break
            credit[best[0]] += inc
            bad[best[0]] = best[1]
            total += inc

        return self._summarise(
            credit,
            scen,
            bad,
            budget=budget_usd,
            book_seed=book_seed,
            increment=inc,
        )

    def _summarise(
        self,
        credit: dict[str, float],
        scen: dict[str, Scenarios],
        bad: dict[str, np.ndarray],
        budget: float,
        book_seed: int,
        increment: float | None = None,
    ) -> HubResult:
        level = self.sim.cvar_level
        hub_bad = sum(bad.values())
        hub_cvar = _cvar(hub_bad, level)
        standalone = {name: _cvar(arr, level) for name, arr in bad.items()}

        inc = increment or max(self.hub_balance / 40.0, 1.0)
        marginal = {}
        for sp in self.spokes:
            bumped = self._spoke_bad_debt(
                sp, scen[sp.name], credit[sp.name] + inc, book_seed
            )
            hub_bumped = bumped + sum(
                bad[o.name] for o in self.spokes if o.name != sp.name
            )
            marginal[sp.name] = (_cvar(hub_bumped, level) - hub_cvar) / inc

        return HubResult(
            allocation=dict(credit),
            hub_cvar=hub_cvar,
            standalone_cvar=standalone,
            marginal_cvar=marginal,
            total_allocated=float(sum(credit.values())),
            budget=budget,
            diversification_benefit=float(sum(standalone.values()) - hub_cvar),
        )
