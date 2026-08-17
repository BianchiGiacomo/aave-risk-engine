"""Run the single-Spoke demo and write figures."""

from __future__ import annotations

import os
from dataclasses import replace

import matplotlib.pyplot as plt
import numpy as np

from . import plotting
from .config import ScenarioConfig, SimConfig
from .engine import RiskEngine


def _fmt(x: float) -> str:
    for unit, div in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(x) >= div:
            return f"${x / div:,.2f}{unit}"
    return f"${x:,.0f}"


def main() -> None:
    cfg = ScenarioConfig(sim=SimConfig(n_scenarios=10_000, seed=7, chunk_size=2_000))
    engine = RiskEngine(cfg)
    base = engine.run()

    print("Aave Collateral Risk-Budgeting Engine")
    print(f"Base exposure: {_fmt(base.total_debt)}")
    for key, value in base.summary().items():
        if "prob" in key or "slippage" in key:
            print(f"  {key:<16}: {value:.2%}")
        else:
            print(f"  {key:<16}: {_fmt(value)}")

    budget = 5_000_000.0
    rec = engine.recommend_cap(
        budget_usd=budget,
        cap_min=2e7,
        cap_max=cfg.positions.total_debt_usd * 2.0,
        n_grid=18,
    )
    print(f"\nRisk budget CVaR{cfg.sim.cvar_level:.0%}: {_fmt(budget)}")
    print(f"Recommended cap / credit line: {_fmt(rec['recommended_cap'])}")

    print("\nReturn-law comparison at base exposure:")
    for model in ("gaussian", "student_t", "jump_diffusion"):
        e = RiskEngine(replace(cfg, stress=replace(cfg.stress, return_model=model)))
        r = e.run()
        print(f"  {model:<15}: CVaR {_fmt(r.cvar)}  P(loss) {r.prob_bad_debt:.2%}")

    lt_sweep = engine.ltv_sweep(np.linspace(0.70, 0.92, 18))
    outdir = os.path.join(os.path.dirname(__file__), "figures")
    docs_assets = os.path.join(os.path.dirname(__file__), "docs", "assets")
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(docs_assets, exist_ok=True)
    figs = {
        "slippage_curve.png": plotting.plot_slippage_curve(engine),
        "loss_distribution.png": plotting.plot_loss_distribution(base),
        "cap_budget.png": plotting.plot_cap_budget(rec["sweep"], budget, rec["recommended_cap"]),
        "ltv_sweep.png": plotting.plot_ltv_sweep(lt_sweep),
    }
    for name, fig in figs.items():
        path = os.path.join(outdir, name)
        fig.savefig(path, dpi=130)
        if name != "ltv_sweep.png":
            published_path = os.path.join(docs_assets, name)
            fig.savefig(published_path, dpi=130)
            print(f"wrote {published_path}")
        plt.close(fig)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
