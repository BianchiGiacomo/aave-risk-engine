"""Matplotlib figures for script output."""

from __future__ import annotations

import numpy as np
import matplotlib.pyplot as plt

from .engine import RiskEngine, RiskResult
from .slippage import calibrate_liquidity, slippage


def _fmt_usd(x: float) -> str:
    for unit, div in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(x) >= div:
            return f"${x / div:.2f}{unit}"
    return f"${x:.0f}"


def plot_slippage_curve(engine: RiskEngine, max_notional_usd: float | None = None):
    cfg = engine.config
    l0 = calibrate_liquidity(
        cfg.asset.spot_price,
        cfg.liquidity.ref_notional_usd,
        cfg.liquidity.ref_slippage,
    )
    l_stress = l0 * (1.0 - cfg.stress.max_depth_haircut * 0.6)
    max_n = max_notional_usd or cfg.liquidity.ref_notional_usd * 8
    notional = np.linspace(0, max_n, 300)

    fig, ax = plt.subplots(figsize=(7, 4.2))
    ax.plot(notional / 1e6, 100 * slippage(notional, l0, cfg.asset.spot_price), label="quiet")
    ax.plot(
        notional / 1e6,
        100 * slippage(notional, l_stress, cfg.asset.spot_price),
        label="stressed depth",
        ls="--",
    )
    bonus_break_even = cfg.risk.liquidation_bonus / (1.0 + cfg.risk.liquidation_bonus)
    ax.axhline(100 * bonus_break_even, color="crimson", ls=":", label="liquidator break-even")
    ax.set_xlabel("liquidation notional sold ($m)")
    ax.set_ylabel("execution slippage (%)")
    ax.set_title("Liquidation slippage")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    return fig


def plot_loss_distribution(result: RiskResult):
    fig, ax = plt.subplots(figsize=(7, 4.2))
    losses = result.bad_debt[result.bad_debt > 0] / 1e6
    if losses.size:
        ax.hist(losses, bins=60, color="steelblue", alpha=0.8)
    ax.axvline(result.var / 1e6, color="darkorange", label=f"VaR = {_fmt_usd(result.var)}")
    ax.axvline(result.cvar / 1e6, color="crimson", label=f"CVaR = {_fmt_usd(result.cvar)}")
    ax.set_xlabel("bad debt ($m)")
    ax.set_ylabel("scenario count")
    ax.set_title(f"Bad-debt distribution | P(loss) = {result.prob_bad_debt:.1%}")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    return fig


def plot_cap_budget(sweep: dict, budget_usd: float, recommended_cap: float):
    fig, ax = plt.subplots(figsize=(7, 4.6))
    ax.plot(sweep["caps"] / 1e6, sweep["cvar"] / 1e6, color="crimson", label="CVaR")
    ax.plot(sweep["caps"] / 1e6, sweep["mean"] / 1e6, color="steelblue", ls="--", label="mean")
    ax.axhline(budget_usd / 1e6, color="green", ls=":", label="budget")
    if np.isfinite(recommended_cap):
        ax.axvline(recommended_cap / 1e6, color="black", label="safe cap")
    ax.set_xlabel("borrow cap / credit line ($m)")
    ax.set_ylabel("bad debt ($m)")
    ax.set_title("Risk budget")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    return fig


def plot_ltv_sweep(sweep: dict):
    fig, ax = plt.subplots(figsize=(7, 4.2))
    ax.plot(100 * sweep["ltvs"], sweep["cvar"] / 1e6, color="crimson")
    ax.set_xlabel("liquidation threshold (%)")
    ax.set_ylabel("CVaR bad debt ($m)")
    ax.set_title("Liquidation-threshold sensitivity")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig
