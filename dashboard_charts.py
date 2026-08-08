"""Plotly charts for the Streamlit dashboard."""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

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
        reaches_sweep_limit = bool(
            np.isclose(recommended_cap, sweep["caps"][-1])
            and sweep["cvar"][-1] <= budget_usd
        )
        fig.add_vline(
            x=recommended_cap / 1e6,
            line=dict(color="black"),
            annotation_text="sweep limit" if reaches_sweep_limit else "budget cap",
            annotation_position="bottom right" if reaches_sweep_limit else "top left",
        )
    fig.update_layout(
        title="Risk budgeting: credit line vs CVaR",
        xaxis_title="borrow cap / credit line ($m)",
        yaxis_title="bad debt ($m)",
        hovermode="x unified",
    )
    return fig


def loss_distribution_fig(result: RiskResult) -> go.Figure:
    positive_usd = result.bad_debt[result.bad_debt > 0]
    positive_label = f"{positive_usd.size:,} positive draw"
    if positive_usd.size != 1:
        positive_label += "s"
    max_loss = float(positive_usd.max()) if positive_usd.size else 0.0
    if max_loss >= 2e6:
        scale, unit = 1e6, "$m"
    elif max_loss >= 2e3:
        scale, unit = 1e3, "$k"
    else:
        scale, unit = 1.0, "$"
    losses = positive_usd / scale
    fig = go.Figure()
    if losses.size == 1:
        fig.add_trace(
            go.Scatter(
                x=losses,
                y=[1],
                mode="markers",
                marker=dict(color=_BLUE, size=13),
                name="positive loss",
                hovertemplate="bad debt: %{x:,.2f}<extra></extra>",
            )
        )
        fig.update_yaxes(range=[0, 1.2], tickvals=[0, 1])
    elif losses.size:
        fig.add_trace(go.Histogram(x=losses, nbinsx=60, marker_color=_BLUE, name="loss"))
    fig.add_vline(
        x=result.var / scale,
        line=dict(color="#ff7f0e"),
        annotation_text="VaR99",
        annotation_position="bottom right",
    )
    fig.add_vline(
        x=result.cvar / scale,
        line=dict(color=_RED),
        annotation_text="CVaR99",
        annotation_position="top right",
    )
    fig.update_layout(
        title=(
            f"Bad-debt distribution | P(loss) = {result.prob_bad_debt:.3%} "
            f"| {positive_label}"
        ),
        xaxis_title=f"bad debt ({unit})",
        yaxis_title="scenario count",
        showlegend=False,
    )
    return fig


def slippage_curve_fig(
    engine: RiskEngine,
    max_notional_usd: float | None = None,
    stress_haircut: float = 0.5,
) -> go.Figure:
    cfg = engine.config
    break_even = cfg.risk.liquidation_bonus / (1.0 + cfg.risk.liquidation_bonus)
    fig = go.Figure()
    points = cfg.liquidity.depth_points
    if points and len(points) >= 2:
        observed = np.asarray(points, dtype=float)
        observed = observed[np.argsort(observed[:, 0])]
        quiet_notional = observed[:, 0]
        stressed_notional = quiet_notional * (1.0 - stress_haircut)
        max_n = max_notional_usd or float(quiet_notional.max())
        scale, unit = (1e6, "$m") if max_n >= 2e6 else (1e3, "$k")
        fig.add_trace(
            go.Scatter(
                x=quiet_notional / scale,
                y=100 * observed[:, 1],
                mode="lines+markers",
                name="quiet depth",
                line=dict(color=_BLUE),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=stressed_notional / scale,
                y=100 * observed[:, 1],
                mode="lines+markers",
                name=f"{stress_haircut:.0%} depth haircut",
                line=dict(color=_RED, dash="dash"),
            )
        )
        x_min = max(float(stressed_notional.min()) / scale * 0.8, 1e-9)
        x_max = float(quiet_notional.max()) / scale * 1.25
        fig.update_xaxes(type="log", range=[np.log10(x_min), np.log10(x_max)])
        x_title = f"notional sold ({unit}, log scale)"
        title = "Empirical liquidation slippage"
    else:
        l0 = calibrate_liquidity(
            cfg.asset.spot_price,
            cfg.liquidity.ref_notional_usd,
            cfg.liquidity.ref_slippage,
        )
        l_stress = l0 * (1.0 - stress_haircut)
        max_n = max_notional_usd or cfg.liquidity.ref_notional_usd * 8
        notional = np.linspace(0, max_n, 300)
        fig.add_trace(
            go.Scatter(
                x=notional / 1e6,
                y=100 * slippage(notional, l0, cfg.asset.spot_price),
                name="quiet depth",
                line=dict(color=_BLUE),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=notional / 1e6,
                y=100 * slippage(notional, l_stress, cfg.asset.spot_price),
                name=f"{stress_haircut:.0%} depth haircut",
                line=dict(color=_RED, dash="dash"),
            )
        )
        x_title = "notional sold ($m)"
        title = "Fitted liquidation slippage"
    fig.add_hline(
        y=100 * break_even,
        line=dict(color="#111111", dash="dot"),
        annotation_text="liquidator break-even",
    )
    fig.update_layout(
        title=title,
        xaxis_title=x_title,
        yaxis_title="slippage (%)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    return fig


def ltv_sweep_fig(sweep: dict) -> go.Figure:
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    thresholds = 100 * sweep["thresholds"]
    fig.add_trace(
        go.Scatter(
            x=thresholds,
            y=sweep["cvar"] / 1e6,
            name="CVaR99",
            line=dict(color=_RED),
        ),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=thresholds,
            y=100 * sweep["prob"],
            name="P(bad debt)",
            line=dict(color=_BLUE, dash="dash"),
        ),
        secondary_y=True,
    )
    fig.add_vline(
        x=100 * sweep["current_threshold"],
        line=dict(color="#111111", dash="dot"),
        annotation_text="current LT",
    )
    fig.update_layout(
        title="Current-book liquidation-threshold sensitivity",
        xaxis_title="liquidation threshold (%)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    fig.update_yaxes(title_text="CVaR99 bad debt ($m)", secondary_y=False)
    fig.update_yaxes(title_text="P(bad debt) (%)", secondary_y=True)
    return fig


def scenario_scatter_fig(engine: RiskEngine, result: RiskResult, max_points: int = 5000) -> go.Figure:
    sc = engine.scenarios
    all_idx = np.arange(result.bad_debt.size)
    loss_idx = all_idx[result.bad_debt > 0]
    if loss_idx.size > max_points:
        order = np.argsort(result.bad_debt[loss_idx])[-max_points:]
        loss_idx = loss_idx[order]
    zero_idx = all_idx[result.bad_debt <= 0]
    keep_zero_n = min(zero_idx.size, max(0, max_points - loss_idx.size))
    if keep_zero_n < zero_idx.size:
        zero_idx = np.random.default_rng(0).choice(
            zero_idx, size=keep_zero_n, replace=False
        )
    max_loss = float(result.bad_debt[loss_idx].max()) if loss_idx.size else 0.0
    if max_loss >= 2e6:
        scale, unit = 1e6, "$m"
    elif max_loss >= 2e3:
        scale, unit = 1e3, "$k"
    else:
        scale, unit = 1.0, "$"

    def custom_data(indices: np.ndarray) -> np.ndarray:
        return np.column_stack(
            (
                100 * sc.peg_drop[indices],
                100 * sc.depth_haircut[indices],
                sc.coll_price[indices],
                100 * result.slippage[indices],
                (result.queued_usd[indices] if result.queued_usd is not None else np.zeros(indices.size)) / scale,
            )
        )

    hover = (
        "ETH return: %{x:.2f}%<br>"
        f"bad debt ({unit}): "
        "%{y:,.3f}<br>"
        "peg drop: %{customdata[0]:.2f}%<br>depth haircut: %{customdata[1]:.2f}%<br>"
        "collateral price: $%{customdata[2]:,.2f}<br>slippage: %{customdata[3]:.2f}%<br>"
        f"queued sale ({unit}): "
        "%{customdata[4]:,.3f}<extra></extra>"
    )
    fig = go.Figure()
    if zero_idx.size:
        fig.add_trace(
            go.Scattergl(
                x=100 * sc.eth_return[zero_idx],
                y=result.bad_debt[zero_idx] / scale,
                customdata=custom_data(zero_idx),
                mode="markers",
                name="no bad debt",
                marker=dict(size=4, color="#9ca3af", opacity=0.25),
                hovertemplate=hover,
            )
        )
    if loss_idx.size:
        fig.add_trace(
            go.Scattergl(
                x=100 * sc.eth_return[loss_idx],
                y=result.bad_debt[loss_idx] / scale,
                customdata=custom_data(loss_idx),
                mode="markers",
                name="bad debt",
                marker=dict(
                    size=8,
                    color=100 * sc.depth_haircut[loss_idx],
                    colorscale="Inferno",
                    colorbar=dict(title="depth haircut %"),
                    opacity=0.9,
                ),
                hovertemplate=hover,
            )
        )
    fig.update_layout(
        title=(
            "Scenario attribution: ETH return, liquidity stress, and bad debt "
            f"| {loss_idx.size:,} loss draws"
        ),
        xaxis_title="ETH return (%)",
        yaxis_title=f"bad debt ({unit})",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    return fig


def empirical_depth_fig(snapshot, clearance) -> go.Figure:
    """Observed quote ladder with clearance and borrower reference sizes."""
    points = np.asarray(snapshot.depth.points, dtype=float)
    order = np.argsort(points[:, 0])
    notional = points[order, 0]
    slip = points[order, 1]
    max_x = max(float(notional.max()), clearance.largest_borrower_usd)
    scale, unit = (1e6, "$m") if max_x >= 2e6 else (1e3, "$k")
    y_top = 1.08 * max(float(100 * slip.max()), 100 * clearance.breakeven_slippage)

    fig = go.Figure()
    if clearance.largest_borrower_usd > notional.max():
        fig.add_trace(
            go.Scatter(
                x=[
                    float(notional.max()) / scale,
                    clearance.largest_borrower_usd / scale,
                    clearance.largest_borrower_usd / scale,
                    float(notional.max()) / scale,
                ],
                y=[0, 0, y_top, y_top],
                fill="toself",
                fillcolor="rgba(153, 153, 153, 0.10)",
                line=dict(color="rgba(0, 0, 0, 0)"),
                name="outside quoted range",
                hoverinfo="skip",
            )
        )
    fig.add_trace(
        go.Scatter(
            x=notional / scale,
            y=100 * slip,
            mode="lines+markers",
            name=f"{snapshot.depth.source} quotes",
            line=dict(color=_BLUE, width=2),
            marker=dict(size=8),
        )
    )
    fig.add_hline(
        y=100 * clearance.breakeven_slippage,
        line=dict(color=_RED, dash="dot"),
        annotation_text="break-even",
    )
    fig.add_trace(
        go.Scatter(
            x=[
                clearance.max_clearable_usd_quiet / scale,
                clearance.max_clearable_usd_quiet / scale,
            ],
            y=[0, 100 * clearance.breakeven_slippage],
            mode="lines",
            name="max clearable",
            line=dict(color=_GREEN, dash="dash"),
            hovertemplate="max clearable: %{x:,.2f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=[clearance.max_clearable_usd_quiet / scale],
            y=[100 * clearance.breakeven_slippage],
            mode="markers",
            showlegend=False,
            marker=dict(color=_GREEN, size=9),
            hovertemplate="max clearable: %{x:,.2f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=[
                clearance.largest_borrower_usd / scale,
                clearance.largest_borrower_usd / scale,
            ],
            y=[0, y_top],
            mode="lines",
            name="largest borrower sale",
            line=dict(color="#111111", dash="dot"),
            hovertemplate="largest sale: %{x:,.2f}<extra></extra>",
        )
    )
    x_min = max(float(notional.min()) / scale * 0.8, 1e-9)
    x_max = max_x / scale * 1.25
    fig.update_xaxes(
        type="log",
        range=[np.log10(x_min), np.log10(x_max)],
    )
    fig.update_yaxes(range=[0, y_top])
    fig.update_layout(
        title=f"{snapshot.chain.title()} {snapshot.reserve.symbol} liquidation depth",
        xaxis_title=f"notional sold ({unit}, log scale)",
        yaxis_title="slippage (%)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=45, r=20, t=85, b=45),
    )
    return fig


def concentration_fig(rows: list[dict], limit: int = 10) -> go.Figure:
    top = rows[:limit][::-1]
    labels = [row["Account"][:8] + "..." + row["Account"][-4:] for row in top]
    debt = [row["Debt"] / 1e6 for row in top]
    colors = [100 * row["ETH debt share"] for row in top]
    fig = go.Figure(
        go.Bar(
            x=debt,
            y=labels,
            orientation="h",
            marker=dict(color=colors, colorscale="Bluered", colorbar=dict(title="ETH debt %")),
            text=[f"${value:,.1f}m" for value in debt],
            textposition="outside",
        )
    )
    fig.update_layout(
        title="Largest target-dominant accounts",
        xaxis_title="debt ($m)",
        yaxis_title="",
        showlegend=False,
        margin=dict(l=85, r=35, t=55, b=45),
    )
    return fig


def mechanics_cvar_fig(rows: list[dict]) -> go.Figure:
    fig = go.Figure()
    for queue, color in (("Aggregate", _BLUE), ("Ordered", _GREEN)):
        selected = [row for row in rows if row["Queue"] == queue]
        fig.add_trace(
            go.Bar(
                name=queue,
                x=[row["Mechanics"] for row in selected],
                y=[row["CVaR99"] / 1e6 for row in selected],
                marker_color=color,
                text=[_fmt_usd(row["CVaR99"]) for row in selected],
                textposition="outside",
            )
        )
    fig.update_layout(
        title="CVaR99 by liquidation mechanics",
        yaxis_title="CVaR99 ($m)",
        barmode="group",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=45, r=20, t=85, b=65),
    )
    return fig


def episode_loss_fig(rows: list[dict]) -> go.Figure:
    fig = go.Figure()
    for depth, color in (("Quiet", _BLUE), ("50% haircut", _RED)):
        selected = [row for row in rows if row["Depth"] == depth]
        fig.add_trace(
            go.Bar(
                name=depth,
                x=[row["Episode"] for row in selected],
                y=[row["Worst bad debt"] / 1e6 for row in selected],
                marker_color=color,
                text=[_fmt_usd(row["Worst bad debt"]) for row in selected],
                textposition="outside",
            )
        )
    fig.update_layout(
        title="Worst historical replay loss",
        yaxis_title="bad debt ($m)",
        barmode="group",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=45, r=20, t=85, b=65),
    )
    return fig


def multiperiod_cvar_fig(rows: list[dict]) -> go.Figure:
    fig = go.Figure()
    for path, color in (("Single shock", _RED), ("Multi-period", _GREEN)):
        selected = [row for row in rows if row["Path"] == path]
        fig.add_trace(
            go.Bar(
                name=path,
                x=[row["Mechanics"] for row in selected],
                y=[row["CVaR99"] / 1e6 for row in selected],
                marker_color=color,
                text=[_fmt_usd(row["CVaR99"]) for row in selected],
                textposition="outside",
            )
        )
    fig.update_layout(
        title="Single shock versus evolving path",
        yaxis_title="CVaR99 ($m)",
        barmode="group",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=45, r=20, t=85, b=65),
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
