"""Matplotlib figures for script output."""

from __future__ import annotations

import numpy as np
import matplotlib.pyplot as plt

from .engine import RiskEngine, RiskResult
from .slippage import calibrate_liquidity, empirical_slippage, slippage


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


def plot_empirical_depth_curve(
    points: list[list[float]],
    bonus_break_even: float,
    marker_notional_usd: float | None = None,
    marker_label: str | None = None,
    additional_markers: list[tuple[float, str]] | None = None,
    quote_label: str = "aggregator quotes",
    title: str = "Empirical market depth",
):
    """Plot observed aggregator depth points and a liquidation threshold."""
    observed = np.asarray(points, dtype=float)
    if observed.ndim != 2 or observed.shape[1] != 2 or observed.shape[0] < 2:
        raise ValueError("need at least two [notional, slippage] points")
    order = np.argsort(observed[:, 0])
    notional = observed[order, 0]
    slip = observed[order, 1]

    fig, ax = plt.subplots(figsize=(7, 4.2))
    ax.plot(
        notional / 1e3,
        100 * slip,
        color="steelblue",
        marker="o",
        label=quote_label,
    )
    ax.axhline(
        100 * bonus_break_even,
        color="crimson",
        ls=":",
        label="liquidator break-even",
    )
    if marker_notional_usd is not None:
        marker_slip = float(empirical_slippage(marker_notional_usd, points))
        ax.scatter(
            [marker_notional_usd / 1e3],
            [100 * marker_slip],
            color="black",
            zorder=3,
            label=marker_label,
        )
    for value, label in additional_markers or []:
        marker_slip = float(empirical_slippage(value, points))
        ax.scatter(
            [value / 1e3],
            [100 * marker_slip],
            color="darkorange",
            marker="X",
            s=70,
            zorder=4,
            label=label,
        )
    ax.set_xscale("log")
    ax.set_xticks(notional / 1e3)
    ax.set_xticklabels([f"{value / 1e3:,.0f}" for value in notional])
    ax.set_xlabel("liquidation notional sold ($k)")
    ax.set_ylabel("execution slippage (%)")
    ax.set_title(title)
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


def plot_time_to_exit_capacity(
    horizon_labels: list[str],
    series: dict[str, list[float]],
    largest_sale_usd: float,
    title: str = "Liquidation capacity by exit horizon",
):
    """Plot cumulative liquidation capacity against the largest sale."""
    x = np.arange(len(horizon_labels))
    styles = {
        "quiet DEX": ("steelblue", ":"),
        "stressed DEX": ("darkorange", ":"),
        "quiet + redemption": ("seagreen", "-"),
        "stressed + redemption": ("crimson", "-"),
    }
    fig, ax = plt.subplots(figsize=(8.2, 4.8))
    for label, values in series.items():
        color, linestyle = styles.get(label, (None, "-"))
        ax.plot(
            x,
            np.asarray(values) / 1e6,
            marker="o",
            color=color,
            linestyle=linestyle,
            label=label,
        )
    ax.axhline(
        largest_sale_usd / 1e6,
        color="black",
        linewidth=1.3,
        label=f"largest sale ({_fmt_usd(largest_sale_usd)})",
    )
    ax.set_xticks(x)
    ax.set_xticklabels(horizon_labels)
    ax.set_xlabel("exit horizon")
    ax.set_ylabel("cumulative capacity ($m)")
    ax.set_title(title)
    ax.grid(alpha=0.3)
    ax.legend(ncols=2)
    fig.tight_layout()
    return fig


def plot_liquidator_balance_sheet_sensitivity(
    canonical_losses: list[float],
    economic_profit: dict[str, list[float]],
    required_bonus: dict[str, list[float]],
    current_bonus: float,
    title: str = "Liquidator economics under canonical recovery risk",
):
    """Plot economic profit and required bonus across canonical impairment."""
    x = 100.0 * np.asarray(canonical_losses, dtype=float)
    styles = {
        "quiet DEX": ("steelblue", ":"),
        "stressed DEX": ("crimson", ":"),
        "quiet + redemption": ("seagreen", "-"),
        "stressed + redemption": ("darkorange", "-"),
    }
    fig, (profit_ax, bonus_ax) = plt.subplots(
        2, 1, figsize=(8.2, 7.0), sharex=True
    )
    for label, values in economic_profit.items():
        color, linestyle = styles.get(label, (None, "-"))
        profit_ax.plot(
            x,
            np.asarray(values, dtype=float) / 1e6,
            marker="o",
            color=color,
            linestyle=linestyle,
            label=label,
        )
    profit_ax.axhline(0.0, color="black", linewidth=1.0)
    profit_ax.set_ylabel("profit after hurdle ($m)")
    profit_ax.set_title(title)
    profit_ax.grid(alpha=0.3)
    profit_ax.legend()

    for label, values in required_bonus.items():
        color, linestyle = styles.get(label, (None, "-"))
        bonus_ax.plot(
            x,
            100.0 * np.asarray(values, dtype=float),
            marker="o",
            color=color,
            linestyle=linestyle,
            label=label,
        )
    bonus_ax.axhline(
        100.0 * current_bonus,
        color="black",
        linewidth=1.0,
        linestyle=":",
        label=f"current bonus ({100.0 * current_bonus:.2f}%)",
    )
    bonus_ax.set_xlabel("canonical/oracle-to-recovery loss (%)")
    bonus_ax.set_ylabel("minimum bonus (%)")
    bonus_ax.grid(alpha=0.3)
    bonus_ax.legend()
    fig.tight_layout()
    return fig
