"""Interactive Streamlit dashboard for the Aave risk engine."""

from __future__ import annotations

import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import streamlit as st

from aave_risk_engine.config import (
    AssetParams,
    LiquidityParams,
    PositionConfig,
    RiskParams,
    ScenarioConfig,
    SimConfig,
    StressConfig,
)
from aave_risk_engine.engine import RiskEngine
from aave_risk_engine.hub import Hub, Spoke
from aave_risk_engine import dashboard_charts as charts


def _fmt_usd(x: float) -> str:
    if not np.isfinite(x):
        return "n/a"
    for unit, div in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(x) >= div:
            return f"${x / div:,.2f}{unit}"
    return f"${x:,.0f}"


def _single_config(p: dict) -> ScenarioConfig:
    return ScenarioConfig(
        asset=AssetParams(name=p["asset"], spot_price=p["spot"]),
        risk=RiskParams(
            ltv=p["ltv"],
            liquidation_threshold=p["lt"],
            liquidation_bonus=p["bonus"],
        ),
        liquidity=LiquidityParams(
            ref_notional_usd=p["depth"],
            ref_slippage=p["ref_slip"],
        ),
        stress=StressConfig(
            horizon_days=p["horizon"],
            eth_annual_vol=p["vol"],
            tail_dof=p["dof"],
            return_model=p["return_model"],
            jump_intensity=p["jump_intensity"],
            jump_mean=p["jump_mean"],
            jump_vol=p["jump_vol"],
            peg_crash_beta=p["peg_beta"],
            depth_crash_beta=p["depth_beta"],
        ),
        positions=PositionConfig(
            total_debt_usd=p["exposure"],
            hf0_median=p["hf0_med"],
            hf0_sigma=p["hf0_sigma"],
        ),
        sim=SimConfig(n_scenarios=p["n_scen"], seed=p["seed"], chunk_size=2_000),
    )


def _single_sidebar() -> dict:
    st.sidebar.title("Single-Spoke parameters")
    st.sidebar.subheader("Market")
    asset = st.sidebar.text_input("Collateral asset", "stETH")
    spot = st.sidebar.number_input("Spot price ($)", 1.0, 1e6, 3000.0, step=100.0)
    return_model = st.sidebar.selectbox("Return law", ["student_t", "jump_diffusion", "gaussian"])
    vol = st.sidebar.slider("Annual volatility", 0.10, 2.00, 0.75, 0.05)
    dof = st.sidebar.slider("Student-t dof", 2.5, 12.0, 3.0, 0.5)
    horizon = st.sidebar.slider("Stress horizon (days)", 0.5, 10.0, 2.0, 0.5)

    jump_intensity, jump_mean, jump_vol = 4.0, -0.15, 0.10
    if return_model == "jump_diffusion":
        jump_intensity = st.sidebar.slider("Jump intensity / year", 0.0, 20.0, 4.0, 0.5)
        jump_mean = st.sidebar.slider("Mean log jump", -0.50, 0.0, -0.15, 0.01)
        jump_vol = st.sidebar.slider("Jump vol", 0.0, 0.40, 0.10, 0.01)

    st.sidebar.subheader("Risk")
    lt = st.sidebar.slider("Liquidation threshold", 0.50, 0.95, 0.85, 0.01)
    ltv = st.sidebar.slider("Origination LTV", 0.40, lt, min(0.80, lt - 0.05), 0.01)
    bonus = st.sidebar.slider("Liquidation bonus", 0.01, 0.20, 0.05, 0.005)

    st.sidebar.subheader("Liquidity and stress coupling")
    depth = st.sidebar.number_input("Reference sell depth ($)", 1e6, 5e8, 25e6, step=5e6)
    ref_slip = st.sidebar.slider("Reference slippage", 0.005, 0.10, 0.02, 0.005)
    peg_beta = st.sidebar.slider("Peg-crash sensitivity", 0.0, 1.0, 0.25, 0.05)
    depth_beta = st.sidebar.slider("Depth-crash sensitivity", 0.0, 3.0, 1.2, 0.1)

    st.sidebar.subheader("Book and budget")
    exposure = st.sidebar.number_input("Current exposure / debt ($)", 1e7, 2e9, 200e6, step=1e7)
    hf0_med = st.sidebar.slider("Median origination HF", 1.05, 3.0, 1.6, 0.05)
    hf0_sigma = st.sidebar.slider("HF dispersion", 0.10, 1.0, 0.45, 0.05)
    budget = st.sidebar.number_input("CVaR budget ($)", 1e5, 1e8, 5e6, step=5e5)
    cap_max = st.sidebar.number_input("Cap sweep max ($)", 5e7, 2e9, 400e6, step=5e7)

    st.sidebar.subheader("Simulation")
    n_scen = st.sidebar.select_slider("Scenarios", [5000, 10000, 20000, 40000], value=10000)
    seed = int(st.sidebar.number_input("Seed", 0, 9999, 7, step=1))

    return locals()


def _run_single(p: dict) -> dict:
    cfg = _single_config(p)
    engine = RiskEngine(cfg)
    base = engine.run()
    rec = engine.recommend_cap(p["budget"], cap_min=2e7, cap_max=p["cap_max"], n_grid=18)
    lt_sweep = engine.ltv_sweep(np.linspace(0.70, 0.92, 18))
    return {"engine": engine, "base": base, "rec": rec, "lt_sweep": lt_sweep, "budget": p["budget"]}


def single_spoke_tab() -> None:
    st.caption("Single-market credit-line sizing. Parameters are in the sidebar.")
    params = _single_sidebar()
    if st.sidebar.button("Run simulation", type="primary"):
        with st.spinner("Running single-Spoke simulation..."):
            st.session_state["single_results"] = _run_single(params)

    if "single_results" not in st.session_state:
        st.info("Set parameters in the sidebar and press **Run simulation**.")
        return

    res = st.session_state["single_results"]
    base = res["base"]
    rec = res["rec"]
    budget = res["budget"]
    engine = res["engine"]

    st.subheader("Headline risk")
    cols = st.columns(5)
    cols[0].metric("Exposure", _fmt_usd(base.total_debt))
    cols[1].metric("P(bad debt)", f"{base.prob_bad_debt:.1%}")
    cols[2].metric("VaR99", _fmt_usd(base.var))
    cols[3].metric("CVaR99", _fmt_usd(base.cvar))
    cols[4].metric("Worst", _fmt_usd(base.worst))

    st.subheader("Decision")
    d1, d2 = st.columns(2)
    d1.metric("Risk budget", _fmt_usd(budget))
    d2.metric("Recommended max-safe cap", _fmt_usd(rec["recommended_cap"]))

    left, right = st.columns(2)
    with left:
        st.plotly_chart(charts.cap_budget_fig(rec["sweep"], budget, rec["recommended_cap"]), use_container_width=True)
        st.plotly_chart(charts.slippage_curve_fig(engine), use_container_width=True)
    with right:
        st.plotly_chart(charts.loss_distribution_fig(base), use_container_width=True)
        st.plotly_chart(charts.ltv_sweep_fig(res["lt_sweep"]), use_container_width=True)

    st.subheader("Worst-case scenario inspector")
    st.plotly_chart(charts.scenario_scatter_fig(engine, base), use_container_width=True)
    sc = engine.scenarios
    idx = int(np.argmax(base.bad_debt))
    bonus = engine.config.risk.liquidation_bonus
    stalled = base.slippage[idx] > bonus / (1.0 + bonus)
    st.write(
        f"Worst scenario: ETH return {100 * sc.eth_return[idx]:.1f}%, "
        f"peg drop {100 * sc.peg_drop[idx]:.1f}%, "
        f"depth haircut {100 * sc.depth_haircut[idx]:.1f}%, "
        f"slippage {100 * base.slippage[idx]:.1f}%"
        + (" (above liquidator break-even; stalled)" if stalled else "")
        + f", bad debt {_fmt_usd(base.bad_debt[idx])}."
    )


_DEFAULT_SPOKES = pd.DataFrame([
    {"spoke": "stETH", "spot $": 3000.0, "vol": 0.80, "rho": 0.95, "LT": 0.86, "LTV": 0.82, "bonus": 0.05, "depth $m": 25.0, "peg_beta": 0.25},
    {"spoke": "WBTC", "spot $": 60000.0, "vol": 0.72, "rho": 0.85, "LT": 0.83, "LTV": 0.79, "bonus": 0.06, "depth $m": 45.0, "peg_beta": 0.00},
    {"spoke": "LONGTAIL", "spot $": 5.0, "vol": 1.05, "rho": 0.45, "LT": 0.72, "LTV": 0.66, "bonus": 0.09, "depth $m": 6.0, "peg_beta": 0.00},
])


def _spokes_from_df(df: pd.DataFrame) -> list[Spoke]:
    spokes = []
    for _, row in df.iterrows():
        name = str(row["spoke"]).strip()
        if not name:
            continue
        base = ScenarioConfig()
        peg_beta = float(row["peg_beta"])
        cfg = replace(
            base,
            asset=replace(base.asset, name=name, spot_price=float(row["spot $"])),
            risk=RiskParams(
                ltv=float(row["LTV"]),
                liquidation_threshold=float(row["LT"]),
                liquidation_bonus=float(row["bonus"]),
            ),
            liquidity=LiquidityParams(ref_notional_usd=float(row["depth $m"]) * 1e6, ref_slippage=0.02),
            stress=replace(
                base.stress,
                eth_annual_vol=float(row["vol"]),
                peg_crash_beta=peg_beta,
                base_peg_drop=0.001 if peg_beta > 0 else 0.0,
            ),
        )
        spokes.append(Spoke(name=name, config=cfg, rho=float(np.clip(row["rho"], 0.0, 1.0))))
    if not spokes:
        raise ValueError("define at least one Spoke")
    return spokes


def hub_tab() -> None:
    st.caption(
        "Allocate one shared Hub balance across Spokes using one systemic factor "
        "and greedy marginal-CVaR allocation."
    )
    c1, c2, c3 = st.columns(3)
    hub_balance = c1.number_input("Hub balance ($)", 1e8, 5e9, 700e6, step=5e7)
    budget = c2.number_input("Hub CVaR budget ($)", 1e5, 1e8, 8e6, step=5e5)
    law = c3.selectbox("Systemic factor law", ["jump_diffusion", "student_t", "gaussian"])

    c4, c5, c6 = st.columns(3)
    severity = c4.slider(
        "Systemic stress severity (x Spoke vol)",
        0.5,
        3.0,
        1.0,
        0.1,
        help="The systemic factor is standardized; severity scales each Spoke's volatility.",
    )
    n_scen = c5.select_slider("Scenarios", [5000, 10000, 20000, 40000], value=10000)
    seed = int(c6.number_input("Seed", 0, 9999, 7, step=1))

    edited = st.data_editor(_DEFAULT_SPOKES, num_rows="dynamic", use_container_width=True, hide_index=True)
    if st.button("Run Hub allocation", type="primary"):
        try:
            spokes = _spokes_from_df(edited)
            market = replace(ScenarioConfig().stress, return_model=law)
            hub = Hub(
                spokes,
                market,
                hub_balance=hub_balance,
                sim=SimConfig(n_scenarios=int(n_scen), seed=seed, chunk_size=5_000),
                severity=severity,
            )
            with st.spinner("Allocating Hub liquidity..."):
                result = hub.allocate(budget_usd=budget, increment_usd=hub_balance / 40.0)
            st.session_state["hub_results"] = (result, budget, hub_balance, {s.name: s.rho for s in spokes})
        except Exception as exc:
            st.error(f"Invalid Hub inputs: {exc}")
            return

    if "hub_results" not in st.session_state:
        st.info("Set Hub inputs and press **Run Hub allocation**.")
        return

    result, budget, hub_balance, rhos = st.session_state["hub_results"]
    st.subheader("Hub outcome")
    cols = st.columns(4)
    cols[0].metric("Credit extended", _fmt_usd(result.total_allocated), f"{result.total_allocated / hub_balance:.0%} of balance")
    cols[1].metric("Hub CVaR99", _fmt_usd(result.hub_cvar), f"budget {_fmt_usd(budget)}")
    cols[2].metric("Sum standalone CVaR", _fmt_usd(sum(result.standalone_cvar.values())))
    cols[3].metric("Diversification gain", _fmt_usd(result.diversification_benefit))

    left, right = st.columns(2)
    with left:
        st.plotly_chart(charts.hub_allocation_fig(result), use_container_width=True)
    with right:
        st.plotly_chart(charts.hub_premium_fig(result), use_container_width=True)

    table = pd.DataFrame([
        {
            "spoke": name,
            "rho": rhos.get(name, np.nan),
            "credit line $m": result.allocation[name] / 1e6,
            "standalone CVaR $m": result.standalone_cvar[name] / 1e6,
            "risk premium $k/$1m": result.marginal_cvar[name] * 1e6 / 1e3,
        }
        for name in result.allocation
    ])
    st.dataframe(table, use_container_width=True, hide_index=True)

    with st.expander("How to read this"):
        st.markdown(
            "Credit lines are the decision. The risk premium is the forward "
            "marginal Hub-CVaR per extra dollar; with a greedy discrete allocator "
            "it is approximately equalized across funded Spokes. Diversification "
            "gain is sum of standalone CVaRs minus Hub CVaR."
        )


def main() -> None:
    st.set_page_config(page_title="Aave Risk-Budgeting Engine", layout="wide")
    st.title("Aave Risk-Budgeting Engine")
    tab_single, tab_hub = st.tabs(["Single Spoke", "Hub allocation (V4)"])
    with tab_single:
        single_spoke_tab()
    with tab_hub:
        hub_tab()


if __name__ == "__main__":
    main()
