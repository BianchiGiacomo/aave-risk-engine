"""Plotly charts for the Streamlit dashboard."""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from aave_risk_engine.engine import RiskEngine, RiskResult
from aave_risk_engine.slippage import calibrate_liquidity, slippage

_RED = "#d62728"
_BLUE = "#1f77b4"
_GREEN = "#2ca02c"


def _fmt_usd(x: float) -> str:
    if not np.isfinite(x):
        return "n/a"
    for unit, div in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(x) >= div:
            return f"${x / div:,.2f}{unit}"
    return f"${x:,.0f}"


def cap_budget_fig(sweep: dict, budget_usd: float, recommended_cap: float) -> go.Figure:
    caps = sweep["caps"] / 1e6
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=caps, y=sweep["cvar"] / 1e6, name="CVaR", line=dict(color=_RED)))
    fig.add_trace(go.Scatter(x=caps, y=sweep["mean"] / 1e6, name="mean", line=dict(color=_BLUE, dash="dash")))
    fig.add_hline(y=budget_usd / 1e6, line=dict(color=_GREEN, dash="dot"), annotation_text="budget")
    if np.isfinite(recommended_cap):
        fig.add_vline(x=recommended_cap / 1e6, line=dict(color="black"), annotation_text="max safe")
    fig.update_layout(
        title="Risk budgeting: credit line vs CVaR",
        xaxis_title="borrow cap / credit line ($m)",
        yaxis_title="bad debt ($m)",
        hovermode="x unified",
    )
    return fig


def loss_distribution_fig(result: RiskResult) -> go.Figure:
    losses = result.bad_debt[result.bad_debt > 0] / 1e6
    fig = go.Figure()
    if losses.size:
        fig.add_trace(go.Histogram(x=losses, nbinsx=60, marker_color=_BLUE, name="loss"))
    fig.add_vline(x=result.var / 1e6, line=dict(color="#ff7f0e"), annotation_text="VaR")
    fig.add_vline(x=result.cvar / 1e6, line=dict(color=_RED), annotation_text="CVaR")
    fig.update_layout(
        title=f"Bad-debt distribution | P(loss) = {result.prob_bad_debt:.1%}",
        xaxis_title="bad debt ($m)",
        yaxis_title="scenario count",
        showlegend=False,
    )
    return fig


def slippage_curve_fig(engine: RiskEngine, max_notional_usd: float | None = None) -> go.Figure:
    cfg = engine.config
    l0 = calibrate_liquidity(cfg.asset.spot_price, cfg.liquidity.ref_notional_usd, cfg.liquidity.ref_slippage)
    l_stress = l0 * (1.0 - cfg.stress.max_depth_haircut * 0.6)
    max_n = max_notional_usd or cfg.liquidity.ref_notional_usd * 8
    notional = np.linspace(0, max_n, 300)
    break_even = cfg.risk.liquidation_bonus / (1.0 + cfg.risk.liquidation_bonus)

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=notional / 1e6, y=100 * slippage(notional, l0, cfg.asset.spot_price), name="quiet"))
    fig.add_trace(go.Scatter(x=notional / 1e6, y=100 * slippage(notional, l_stress, cfg.asset.spot_price), name="stressed", line=dict(dash="dash")))
    fig.add_hline(y=100 * break_even, line=dict(color="black", dash="dot"), annotation_text="liquidator break-even")
    fig.update_layout(
        title="Liquidation slippage",
        xaxis_title="notional sold ($m)",
        yaxis_title="slippage (%)",
    )
    return fig


def ltv_sweep_fig(sweep: dict) -> go.Figure:
    fig = go.Figure(go.Scatter(x=100 * sweep["ltvs"], y=sweep["cvar"] / 1e6, line=dict(color=_RED)))
    fig.update_layout(
        title="Sensitivity to liquidation threshold",
        xaxis_title="liquidation threshold (%)",
        yaxis_title="CVaR bad debt ($m)",
        showlegend=False,
    )
    return fig


def scenario_scatter_fig(engine: RiskEngine, result: RiskResult, max_points: int = 5000) -> go.Figure:
    sc = engine.scenarios
    idx = np.arange(result.bad_debt.size)
    if idx.size > max_points:
        rng = np.random.default_rng(0)
        loss_idx = idx[result.bad_debt > 0]
        zero_idx = idx[result.bad_debt <= 0]
        keep_zero = rng.choice(zero_idx, size=max(0, max_points - loss_idx.size), replace=False)
        idx = np.concatenate([loss_idx, keep_zero])

    fig = go.Figure(go.Scattergl(
        x=100 * sc.eth_return[idx],
        y=result.bad_debt[idx] / 1e6,
        mode="markers",
        marker=dict(size=4, color=100 * sc.depth_haircut[idx], colorscale="Inferno", opacity=0.65),
    ))
    fig.update_layout(
        title="Losses by ETH return and depth haircut",
        xaxis_title="ETH return (%)",
        yaxis_title="bad debt ($m)",
    )
    return fig


def hub_allocation_fig(result) -> go.Figure:
    names = list(result.allocation)
    credit = [result.allocation[n] / 1e6 for n in names]
    fig = go.Figure(go.Bar(x=names, y=credit, marker_color=_BLUE, text=[f"${c:,.0f}m" for c in credit], textposition="outside"))
    fig.update_layout(title="Allocated credit line per Spoke", yaxis_title="credit line ($m)")
    return fig


def hub_premium_fig(result) -> go.Figure:
    names = list(result.allocation)
    premium = [result.marginal_cvar[n] * 1e6 / 1e3 for n in names]
    standalone = [result.standalone_cvar[n] / 1e6 for n in names]
    fig = go.Figure()
    fig.add_trace(go.Bar(name="risk premium ($k per $1m)", x=names, y=premium, marker_color=_RED))
    fig.add_trace(go.Bar(name="standalone CVaR ($m)", x=names, y=standalone, marker_color="#9ecae1", yaxis="y2"))
    fig.update_layout(
        title="Risk premium (approximately equalized) vs standalone tail",
        yaxis=dict(title="risk premium ($k per $1m)"),
        yaxis2=dict(title="standalone CVaR ($m)", overlaying="y", side="right", showgrid=False),
        barmode="group",
    )
    return fig
