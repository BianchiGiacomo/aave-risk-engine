"""Interactive real-market dashboard for the Aave risk engine."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import os
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import streamlit as st

from aave_risk_engine import dashboard_charts as charts
from aave_risk_engine.dashboard_analysis import (
    ClearanceExtensionInputs,
    account_rows,
    run_clearance_extension,
    run_episode_analysis,
    run_liquidation_threshold_sensitivity,
    run_market_analysis,
    run_multiperiod_analysis,
    run_v4_analysis,
)
from aave_risk_engine.data import load_snapshot, snapshot_from_json, snapshot_to_json
from aave_risk_engine.data.book import scenario_config_from_snapshot


_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_LOGO_PATH = os.path.join(_PACKAGE_DIR, "aave_logo.png")
_RUNTIME_DIR = os.path.join(_PACKAGE_DIR, ".runtime")
_MARKETS = {
    "Aave V3 Ethereum Core: wstETH reserve": {
        "path": os.path.join(_PACKAGE_DIR, "data", "snapshots", "aave_v3_ethereum_wsteth.json"),
        "chain": "ethereum",
        "asset": "wstETH",
        "pair_dest": "WETH",
        "registry": os.path.join(
            _PACKAGE_DIR, "data", "borrowers", "aave_v3_ethereum.json"
        ),
        "ladder_usd": (25e3, 100e3, 500e3, 2e6, 8e6, 25e6),
        "budget": 5_000_000.0,
    },
    "Aave V3 Linea: WETH reserve": {
        "path": os.path.join(_PACKAGE_DIR, "data", "snapshots", "aave_v3_linea_weth.json"),
        "chain": "linea",
        "asset": "WETH",
        "pair_dest": "USDC",
        "registry": os.path.join(
            _PACKAGE_DIR, "data", "borrowers", "aave_v3_linea.json"
        ),
        "ladder_usd": (5e3, 15e3, 41e3, 100e3, 300e3, 1e6),
        "budget": 500_000.0,
    },
}
_LIVE_REFRESH_TIMEOUT_S = 900
_V4_MODEL_VERSION = "spoke-severity-v2"
_MULTIPERIOD_MODEL_VERSION = "ou-peg-v3"
_CLEARANCE_EXTENSION_VERSION = "route-optimizer-v1"


def _fmt_usd(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "n/a"
    for unit, divisor in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(value) >= divisor:
            return f"${value / divisor:,.2f}{unit}"
    return f"${value:,.0f}"


def _fmt_duration(hours: float | None) -> str:
    if hours is None:
        return "not clearable"
    if hours < 24.0:
        return f"{hours:.1f}h"
    return f"{hours / 24.0:.2f}d"


def _snapshot_date(snapshot) -> str:
    return dt.datetime.fromtimestamp(snapshot.timestamp, dt.timezone.utc).date().isoformat()


def _refresh_snapshot(market: dict):
    """Build a live snapshot in a bounded child process."""
    parent_dir = os.path.dirname(_PACKAGE_DIR)
    os.makedirs(_RUNTIME_DIR, exist_ok=True)
    output_path = os.path.join(_RUNTIME_DIR, f"snapshot_{uuid.uuid4().hex}.json")
    runtime_registry = os.path.join(
        _RUNTIME_DIR, f"borrowers_{market['chain']}.json"
    )
    try:
        command = [
            sys.executable,
            "-m",
            "aave_risk_engine.data.build_snapshot",
            "--chain",
            market["chain"],
            "--asset",
            market["asset"],
            "--pair-dest",
            market["pair_dest"],
            "--ladder-usd",
            ",".join(str(value) for value in market["ladder_usd"]),
            "--rpc-timeout",
            "12",
            "--rpc-retries",
            "1",
            "--borrower-registry",
            market["registry"],
            "--registry-out",
            runtime_registry,
            "--account-cache-dir",
            _RUNTIME_DIR,
            "--account-request-batch-size",
            "100",
            "--out",
            output_path,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=parent_dir,
                capture_output=True,
                text=True,
                timeout=_LIVE_REFRESH_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"public-source refresh exceeded {_LIVE_REFRESH_TIMEOUT_S // 60} minutes"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            message = detail[-1] if detail else f"builder exited {completed.returncode}"
            raise RuntimeError(message)
        return load_snapshot(output_path)
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass


@st.cache_resource(show_spinner=False)
def _cached_market_analysis(
    payload: str,
    scope: str,
    min_target_share: float,
    n_scenarios: int,
    seed: int,
    budget_usd: float,
    ordered: bool,
):
    return run_market_analysis(
        snapshot_from_json(payload),
        scope,
        min_target_share,
        n_scenarios,
        seed,
        budget_usd,
        ordered,
    )


@st.cache_resource(show_spinner=False)
def _cached_v4_analysis(
    payload: str,
    scope: str,
    min_target_share: float,
    n_scenarios: int,
    seed: int,
    model_version: str,
):
    del model_version  # Included in the Streamlit cache key.
    return run_v4_analysis(
        snapshot_from_json(payload), scope, min_target_share, n_scenarios, seed
    )


@st.cache_resource(show_spinner=False)
def _cached_liquidation_threshold_sensitivity(
    payload: str,
    scope: str,
    min_target_share: float,
    n_scenarios: int,
    seed: int,
    ordered: bool,
):
    return run_liquidation_threshold_sensitivity(
        snapshot_from_json(payload),
        scope,
        min_target_share,
        n_scenarios,
        seed,
        ordered,
    )


@st.cache_resource(show_spinner=False)
def _cached_episode_analysis(payload: str, scope: str, min_target_share: float):
    return run_episode_analysis(snapshot_from_json(payload), scope, min_target_share)


@st.cache_resource(show_spinner=False)
def _cached_multiperiod_analysis(
    payload: str,
    scope: str,
    min_target_share: float,
    n_paths: int,
    seed: int,
    n_periods: int,
    total_days: float,
    replenish: float,
    peg_mean_reversion_speed: float,
    model_version: str,
):
    del model_version  # Included in the Streamlit cache key.
    return run_multiperiod_analysis(
        snapshot_from_json(payload),
        scope,
        min_target_share,
        n_paths,
        seed,
        n_periods,
        total_days,
        replenish,
        peg_mean_reversion_speed,
    )


@st.cache_resource(show_spinner=False)
def _cached_clearance_extension(
    payload: str,
    min_target_share: float,
    inputs: ClearanceExtensionInputs,
    model_version: str,
):
    del model_version  # Included in the Streamlit cache key.
    return run_clearance_extension(
        snapshot_from_json(payload), min_target_share, inputs
    )


def _active_snapshot(market_name: str, market: dict):
    state_key = f"snapshot::{market_name}"
    source_key = f"snapshot_source::{market_name}"
    if state_key not in st.session_state:
        st.session_state[state_key] = snapshot_to_json(load_snapshot(market["path"]))
        st.session_state[source_key] = "Committed"

    with st.sidebar.expander("Snapshot data", expanded=True):
        source = st.session_state[source_key]
        active = snapshot_from_json(st.session_state[state_key])
        st.caption(
            f"{source} | block {active.block:,} | {_snapshot_date(active)}"
        )
        st.caption("Live refresh is an explicit network action and may take up to 15 minutes.")
        if st.button("Refresh from public sources", width="stretch"):
            try:
                with st.spinner("Reading Aave state, borrowers, prices, and routed depth..."):
                    refreshed = _refresh_snapshot(market)
                st.session_state[state_key] = snapshot_to_json(refreshed)
                st.session_state[source_key] = "Live session"
                st.success(f"Loaded block {refreshed.block:,}")
            except Exception as exc:  # noqa: BLE001 - keep the committed fallback active
                st.error(f"Refresh failed: {exc}")
        if st.button("Restore committed snapshot", width="stretch"):
            st.session_state[state_key] = snapshot_to_json(load_snapshot(market["path"]))
            st.session_state[source_key] = "Committed"

        payload = st.session_state[state_key]
        snapshot = snapshot_from_json(payload)
        file_name = (
            f"aave_v3_{snapshot.chain}_{snapshot.reserve.symbol.lower()}_"
            f"block_{snapshot.block}.json"
        )
        st.download_button(
            "Download active snapshot",
            data=payload,
            file_name=file_name,
            mime="application/json",
            width="stretch",
        )
    return snapshot_from_json(st.session_state[state_key]), st.session_state[state_key]


def _display_metrics(analysis, snapshot, scope: str) -> None:
    result = analysis["result"]
    book = analysis["book"]
    recommendation = analysis["recommendation"]["recommended_cap"]
    eth_debt = float(book.eth_debt_usd.sum()) if book.eth_debt_usd is not None else 0.0

    reserve = snapshot.reserve
    cap_usage = (
        reserve.total_supplied_tokens / reserve.supply_cap_tokens
        if reserve.supply_cap_tokens > 0
        else float("nan")
    )
    first = st.columns(3)
    first[0].metric("On-chain collateral", _fmt_usd(reserve.supplied_usd))
    first[1].metric("Supply cap usage", f"{cap_usage:.0%}" if np.isfinite(cap_usage) else "n/a")
    first[2].metric("Modeled debt exposure", _fmt_usd(book.total_debt))
    second = st.columns(4)
    second[0].metric("P(bad debt)", f"{result.prob_bad_debt:.3%}")
    second[1].metric("Expected bad debt", _fmt_usd(result.mean))
    second[2].metric("CVaR99", _fmt_usd(result.cvar))
    sweep = analysis["recommendation"]["sweep"]
    reaches_sweep_limit = bool(
        np.isclose(recommendation, sweep["caps"][-1])
        and sweep["cvar"][-1] <= analysis["recommendation"]["budget"]
    )
    second[3].metric(
        "Safe within sweep" if reaches_sweep_limit else "Model-safe exposure",
        _fmt_usd(recommendation),
    )
    loss_count = result.positive_loss_count
    third = st.columns(4)
    third[0].metric("Accounts", f"{book.debt_usd.size:,}")
    third[1].metric("Median health factor", f"{float(np.median(book.hf0)):.2f}")
    third[2].metric("Positive-loss draws", f"{loss_count:,} / {result.bad_debt.size:,}")
    conditional = result.conditional_mean_bad_debt
    third[3].metric(
        "Severity if loss",
        _fmt_usd(conditional) if conditional is not None else "n/a",
    )
    probability_low, probability_high = result.prob_bad_debt_interval()
    st.caption(
        f"P(bad debt) 95% Wilson interval: {probability_low:.3%} to "
        f"{probability_high:.3%}. Expected bad debt equals event probability "
        "times mean severity conditional on a positive loss."
    )
    st.caption(
        f"ETH-denominated debt modeled: {_fmt_usd(eth_debt)}."
        if scope == "Combined"
        else "ETH-debt-dominant loopers are excluded from this book."
    )


def _overview_tab(
    analysis,
    snapshot,
    min_target_share: float,
    budget_usd: float,
    scope: str,
) -> None:
    _display_metrics(analysis, snapshot, scope)
    loss_count = analysis["result"].positive_loss_count
    if loss_count < 30:
        st.warning(
            f"Only {loss_count:,} positive-loss draws were observed. "
            "Treat CVaR and conditional severity as low-sample estimates. "
            "The Wilson interval above quantifies probability uncertainty; "
            "publication-grade tail severity requires targeted rare-event sampling."
        )
    recommendation = analysis["recommendation"]
    left, right = st.columns(2)
    with left:
        st.plotly_chart(
            charts.cap_budget_fig(recommendation["sweep"], budget_usd, recommendation["recommended_cap"]),
            width="stretch",
        )
    with right:
        st.plotly_chart(charts.loss_distribution_fig(analysis["result"]), width="stretch")

    rows = account_rows(snapshot, min_target_share)
    if scope == "USD debt":
        rows = [row for row in rows if row["ETH debt share"] <= 0.5]
    if rows:
        st.plotly_chart(charts.concentration_fig(rows), width="stretch")
        top = pd.DataFrame(rows[:10]).copy()
        top["Debt"] = top["Debt"].map(_fmt_usd)
        top["Collateral"] = top["Collateral"].map(_fmt_usd)
        top["Target share"] = top["Target share"].map(lambda value: f"{value:.0%}")
        top["ETH debt share"] = top["ETH debt share"].map(lambda value: f"{value:.0%}")
        top["Health factor"] = top["Health factor"].map(lambda value: f"{value:.2f}")
        st.dataframe(top, width="stretch", hide_index=True)


def _scenario_parameter_rows(analysis, snapshot, scope: str, min_target_share: float):
    config = analysis["config"]
    risk = config.risk
    stress = config.stress
    liquidity = config.liquidity
    sim = config.sim
    rows = [
        ("Run", "Borrower book", scope, "Dashboard control"),
        ("Run", "Minimum target share", f"{min_target_share:.0%}", "Dashboard control"),
        (
            "Run",
            "Queue clearing",
            "Ordered" if risk.ordered_queue else "Aggregate",
            "Dashboard control",
        ),
        ("Run", "Scenarios", f"{sim.n_scenarios:,}", "Dashboard control"),
        ("Run", "Random seed", f"{sim.seed}", "Dashboard control"),
        ("Run", "Chunk size", f"{sim.chunk_size:,}", "Model configuration"),
        ("Run", "CVaR level", f"{sim.cvar_level:.0%}", "Model configuration"),
        (
            "Run",
            "CVaR budget",
            _fmt_usd(analysis["recommendation"]["budget"]),
            "Dashboard control",
        ),
        ("Asset", "Collateral driver", config.asset.name, "On-chain snapshot"),
        ("Asset", "Spot price", _fmt_usd(config.asset.spot_price), "On-chain snapshot"),
        ("Return", "Horizon", f"{stress.horizon_days:g} days", "Model configuration"),
        ("Return", "Law", stress.return_model, "Model configuration"),
        (
            "Return",
            "Annual volatility",
            f"{stress.eth_annual_vol:.2%}",
            "Historical calibration",
        ),
        ("Return", "Annual drift", f"{stress.eth_annual_drift:.2%}", "Model assumption"),
    ]
    if stress.return_model == "student_t":
        rows.append(
            (
                "Return",
                "Student-t degrees of freedom",
                f"{stress.tail_dof:.3f}",
                "Historical calibration",
            )
        )
    elif stress.return_model == "jump_diffusion":
        rows.extend(
            [
                ("Return", "Jump intensity", f"{stress.jump_intensity:.3f}", "Model assumption"),
                ("Return", "Jump mean", f"{stress.jump_mean:.2%}", "Model assumption"),
                ("Return", "Jump volatility", f"{stress.jump_vol:.2%}", "Model assumption"),
            ]
        )
    rows.extend(
        [
        ("Peg", "Base peg drop", f"{stress.base_peg_drop:.3%}", "Model configuration"),
        ("Peg", "Crash beta", f"{stress.peg_crash_beta:.3f}", "Model assumption"),
        (
            "Peg",
            "Idiosyncratic volatility",
            f"{stress.peg_idio_vol:.3%}",
            "Historical calibration",
        ),
        (
            "Peg",
            "Residual mean-reversion half-life",
            (
                f"{math.log(2.0) / stress.peg_mean_reversion_speed:.2f} days"
                if stress.peg_mean_reversion_speed > 0.0
                else "Disabled"
            ),
            (
                "Historical calibration"
                if snapshot.stress is not None
                and snapshot.stress.peg_mean_reversion_speed is not None
                else "Model assumption"
            ),
        ),
        ("Peg", "Maximum peg drop", f"{stress.max_peg_drop:.1%}", "Model limit"),
        (
            "Liquidity",
            "Base depth haircut",
            f"{stress.base_depth_haircut:.1%}",
            "Model assumption",
        ),
        ("Liquidity", "Crash beta", f"{stress.depth_crash_beta:.3f}", "Model assumption"),
        (
            "Liquidity",
            "Idiosyncratic volatility",
            f"{stress.depth_idio_vol:.1%}",
            "Model assumption",
        ),
        (
            "Liquidity",
            "Maximum depth haircut",
            f"{stress.max_depth_haircut:.1%}",
            "Model limit",
        ),
        (
            "Liquidity",
            "Delayed-liquidation drawdown",
            f"{stress.liquidation_delay_drawdown:.1%}",
            "Model assumption",
        ),
        ("Liquidation", "Reserve LTV", f"{risk.ltv:.2%}", "On-chain snapshot"),
        (
            "Liquidation",
            "Reserve liquidation threshold",
            f"{risk.liquidation_threshold:.2%}",
            "On-chain snapshot",
        ),
        ("Liquidation", "Liquidation bonus", f"{risk.liquidation_bonus:.2%}", "On-chain snapshot"),
        (
            "Liquidation",
            "Liquidator break-even",
            f"{risk.liquidation_bonus / (1 + risk.liquidation_bonus):.2%}",
            "Derived",
        ),
        ("Liquidation", "Close factor", f"{risk.close_factor:.0%}", "Model configuration"),
        (
            "Liquidation",
            "Full-liquidation HF",
            f"{risk.full_liquidation_hf:.3f}",
            "Model configuration",
        ),
        (
            "Book",
            "Modeled accounts",
            f"{analysis['book'].debt_usd.size:,}",
            "Filtered snapshot",
        ),
        ("Book", "Modeled debt", _fmt_usd(analysis["book"].total_debt), "Filtered snapshot"),
        ("Book", "Per-account weighted LT", "Enabled", "On-chain snapshot"),
        ]
    )
    if snapshot.depth is not None:
        notionals = [point[0] for point in snapshot.depth.points]
        rows.extend(
            [
                (
                    "Liquidity",
                    "Quote source",
                    f"{snapshot.depth.source} {snapshot.depth.pair}",
                    "Public aggregator",
                ),
                ("Liquidity", "Empirical quote points", f"{len(notionals)}", "Committed snapshot"),
                (
                    "Liquidity",
                    "Empirical quote range",
                    f"{_fmt_usd(min(notionals))} to {_fmt_usd(max(notionals))}",
                    "Committed snapshot",
                ),
                (
                    "Liquidity",
                    "Fitted reference notional",
                    _fmt_usd(liquidity.ref_notional_usd),
                    "Derived fallback",
                ),
                (
                    "Liquidity",
                    "Fitted reference slippage",
                    f"{liquidity.ref_slippage:.3%}",
                    "Derived fallback",
                ),
            ]
        )
    return [
        {"Group": group, "Parameter": parameter, "Value": value, "Source": source}
        for group, parameter, value, source in rows
    ]


def _sensitivities_tab(
    analysis,
    snapshot,
    payload: str,
    context_key: str,
    scope: str,
    min_target_share: float,
    n_scenarios: int,
    seed: int,
    ordered: bool,
) -> None:
    with st.expander("Active scenario and liquidation parameters", expanded=False):
        st.dataframe(
            pd.DataFrame(
                _scenario_parameter_rows(
                    analysis, snapshot, scope, min_target_share
                )
            ),
            width="stretch",
            hide_index=True,
        )

    st.plotly_chart(
        charts.scenario_scatter_fig(analysis["engine"], analysis["result"]),
        width="stretch",
    )
    st.caption(
        "Every positive-loss draw is retained; no-loss draws are deterministically "
        "subsampled for readability. Hover data exposes peg, depth, price, slippage, "
        "and queued-sale conditions."
    )

    left, right = st.columns(2)
    with left:
        st.plotly_chart(
            charts.slippage_curve_fig(analysis["engine"], stress_haircut=0.5),
            width="stretch",
        )
        st.caption(
            "The stressed curve shifts empirical capacity by a linear 50% depth "
            "haircut; it is a sensitivity, not a second quote observation."
        )
    with right:
        state_key = "dashboard_lt_sensitivity"
        if st.button("Run liquidation-threshold sensitivity", type="primary"):
            with st.spinner("Repricing the current borrower book across target LTs..."):
                st.session_state[state_key] = (
                    context_key,
                    _cached_liquidation_threshold_sensitivity(
                        payload,
                        scope,
                        min_target_share,
                        n_scenarios,
                        seed,
                        ordered,
                    ),
                )
        stored = st.session_state.get(state_key)
        if not stored or stored[0] != context_key:
            st.info("Run the transition sensitivity for the active snapshot and book.")
        else:
            sweep = stored[1]
            st.plotly_chart(charts.ltv_sweep_fig(sweep), width="stretch")
            table = pd.DataFrame(
                {
                    "Target LT": [f"{value:.1%}" for value in sweep["thresholds"]],
                    "CVaR99": [_fmt_usd(value) for value in sweep["cvar"]],
                    "P(bad debt)": [f"{value:.3%}" for value in sweep["prob"]],
                    "Accounts below HF 1": sweep["underwater_accounts"],
                }
            )
            st.dataframe(table, width="stretch", hide_index=True)
        st.caption(
            "This is a V3 current-book or forced V4 migration test. Each account's "
            "weighted LT moves with its target collateral share, so its HF changes "
            "immediately. A normal V4 dynamic-config update instead leaves an existing "
            "position on its prior configuration until it takes a risk-increasing "
            "action, unless governance forces migration. Borrower responses and future "
            "origination are not modeled."
        )


def _clearance_extension_inputs(snapshot) -> tuple[ClearanceExtensionInputs, bool]:
    is_wsteth = snapshot.reserve.symbol.lower() == "wsteth"
    with st.form("clearance_extension_form"):
        with st.expander("Exit and liquidator assumptions", expanded=False):
            horizon_controls = st.columns(4)
            decision_horizon = float(
                horizon_controls[0].select_slider(
                    "Decision horizon (days)",
                    options=[1, 3, 7, 14, 30, 60, 90],
                    value=7,
                )
            )
            quiet_refill = float(
                horizon_controls[1].number_input(
                    "Quiet DEX refill (hours)",
                    min_value=0.5,
                    max_value=168.0,
                    value=6.0,
                    step=0.5,
                )
            )
            stressed_refill = float(
                horizon_controls[2].number_input(
                    "Stressed DEX refill (hours)",
                    min_value=0.5,
                    max_value=720.0,
                    value=24.0,
                    step=1.0,
                )
            )
            depth_haircut = float(
                horizon_controls[3].slider(
                    "Stressed depth haircut (%)", 0, 95, 50, 5
                )
                / 100.0
            )

            if is_wsteth:
                redemption_controls = st.columns(3)
                quiet_redemption = float(
                    redemption_controls[0].number_input(
                        "Quiet redemption ($m/day)",
                        min_value=0.0,
                        max_value=500.0,
                        value=25.0,
                        step=2.5,
                    )
                    * 1e6
                )
                stressed_redemption = float(
                    redemption_controls[1].number_input(
                        "Stressed redemption ($m/day)",
                        min_value=0.0,
                        max_value=500.0,
                        value=25.0,
                        step=2.5,
                    )
                    * 1e6
                )
                redemption_delay = float(
                    redemption_controls[2].number_input(
                        "Redemption delay (hours)",
                        min_value=0.0,
                        max_value=720.0,
                        value=24.0,
                        step=1.0,
                    )
                )
            else:
                quiet_redemption = 0.0
                stressed_redemption = 0.0
                redemption_delay = 0.0
                st.caption(
                    "Primary redemption is not modeled for the selected WETH reserve; "
                    "the extension remains DEX-only."
                )

            loss_controls = st.columns(4)
            canonical_loss = float(
                loss_controls[0].number_input(
                    "Canonical recovery loss (%)",
                    min_value=0.0,
                    max_value=99.0,
                    value=4.0,
                    step=0.5,
                )
                / 100.0
            )
            dex_market_discount = float(
                loss_controls[1].number_input(
                    "Additional DEX discount (%)",
                    min_value=0.0,
                    max_value=99.0,
                    value=0.0,
                    step=0.5,
                )
                / 100.0
            )
            dex_execution_loss = float(
                loss_controls[2].number_input(
                    "DEX execution-loss ceiling (%)",
                    min_value=0.01,
                    max_value=99.0,
                    value=1.0,
                    step=0.25,
                )
                / 100.0
            )
            redemption_loss = float(
                loss_controls[3].number_input(
                    "Redemption loss (%)",
                    min_value=0.0,
                    max_value=99.0,
                    value=0.0,
                    step=0.25,
                    disabled=not is_wsteth,
                )
                / 100.0
            )

            capital_controls = st.columns(4)
            funding = float(
                capital_controls[0].number_input(
                    "Funding annual rate (%)",
                    min_value=0.0,
                    max_value=200.0,
                    value=10.0,
                    step=1.0,
                )
                / 100.0
            )
            hurdle = float(
                capital_controls[1].number_input(
                    "Capital hurdle annual (%)",
                    min_value=0.0,
                    max_value=200.0,
                    value=10.0,
                    step=1.0,
                )
                / 100.0
            )
            hedge_entry = float(
                capital_controls[2].number_input(
                    "Hedge entry cost (%)",
                    min_value=0.0,
                    max_value=20.0,
                    value=0.10,
                    step=0.05,
                )
                / 100.0
            )
            hedge_carry = float(
                capital_controls[3].number_input(
                    "Hedge carry annual (%)",
                    min_value=0.0,
                    max_value=200.0,
                    value=2.0,
                    step=0.5,
                )
                / 100.0
            )

            final_controls = st.columns(4)
            stalled_drawdown = float(
                final_controls[0].number_input(
                    "Unresolved drawdown (%)",
                    min_value=0.0,
                    max_value=100.0,
                    value=10.0,
                    step=1.0,
                )
                / 100.0
            )
            max_horizon = float(
                final_controls[1].number_input(
                    "Maximum warehouse horizon (days)",
                    min_value=1.0,
                    max_value=730.0,
                    value=365.0,
                    step=1.0,
                )
            )
            route_label = final_controls[2].selectbox(
                "Route allocation",
                ["Profit maximizing", "Capacity first"],
                help=(
                    "Profit maximizing selects one total route split. Capacity first "
                    "uses available routes immediately as a benchmark."
                ),
            )
            final_controls[3].metric(
                "Funding + hurdle",
                f"{funding + hurdle:.1%}",
                help="Both rates accrue on the same capital-days base.",
            )

        submitted = st.form_submit_button(
            "Run horizon and economic clearance",
            type="primary",
        )

    return (
        ClearanceExtensionInputs(
            decision_horizon_days=decision_horizon,
            stress_depth_haircut=depth_haircut,
            quiet_refill_hours=quiet_refill,
            stressed_refill_hours=stressed_refill,
            quiet_redemption_usd_per_day=quiet_redemption,
            stressed_redemption_usd_per_day=stressed_redemption,
            redemption_delay_hours=redemption_delay,
            stalled_drawdown=stalled_drawdown,
            funding_annual_rate=funding,
            hurdle_annual_rate=hurdle,
            hedge_entry_cost=hedge_entry,
            hedge_carry_annual_rate=hedge_carry,
            dex_execution_loss=dex_execution_loss,
            redemption_loss=redemption_loss,
            canonical_loss=canonical_loss,
            dex_market_discount=dex_market_discount,
            route_strategy=(
                "profit_maximizing"
                if route_label == "Profit maximizing"
                else "capacity_first"
            ),
            max_horizon_days=max_horizon,
        ),
        submitted,
    )


def _clearance_extension_section(
    snapshot,
    payload: str,
    base_context: str,
    min_target_share: float,
    strict_passes: bool,
) -> None:
    st.divider()
    st.subheader("Horizon and economic clearance")
    st.caption(
        "This extension preserves the strict instant verdict and evaluates the same "
        "largest sale under explicit DEX refill, redemption, capital, and recovery "
        "assumptions. These inputs are sensitivities, not measured liquidator terms."
    )
    inputs, submitted = _clearance_extension_inputs(snapshot)
    context_key = (
        f"{base_context}|{inputs!r}|{_CLEARANCE_EXTENSION_VERSION}"
    )
    state_key = "dashboard_clearance_extension"
    if submitted:
        with st.spinner("Evaluating exit capacity and liquidator economics..."):
            st.session_state[state_key] = (
                context_key,
                _cached_clearance_extension(
                    payload,
                    min_target_share,
                    inputs,
                    _CLEARANCE_EXTENSION_VERSION,
                ),
            )
    stored = st.session_state.get(state_key)
    if not stored or stored[0] != context_key:
        st.info(
            "The strict instant result above is active. The horizon extension runs "
            "only when its explicit assumptions are submitted."
        )
        return

    extension = stored[1]
    preferred = (
        "Quiet + redemption"
        if "Quiet + redemption" in extension["time_curves"]
        else "Quiet DEX"
    )
    horizon_row = next(
        row for row in extension["horizon_rows"] if row["Regime"] == preferred
    )
    economic_row = next(
        row for row in extension["economic_rows"] if row["Regime"] == preferred
    )
    result = economic_row["Result"]
    minimum_bonus = economic_row["Minimum bonus"]
    metrics = st.columns(4)
    metrics[0].metric("Strict instant ARFC", "PASS" if strict_passes else "FAIL")
    metrics[1].metric(
        f"{inputs.decision_horizon_days:g}d capacity test",
        "PASS" if horizon_row["Pass by horizon"] else "FAIL",
        delta=(
            "sale cleared"
            if horizon_row["Pass by horizon"]
            else f"{_fmt_usd(horizon_row['Unresolved sale'])} unresolved"
        ),
        delta_color="off",
    )
    metrics[2].metric(
        "Estimated full exit",
        _fmt_duration(result.time_to_clear_hours),
        delta=preferred,
        delta_color="off",
    )
    metrics[3].metric(
        "Economic clearance",
        "PASS" if result.economic_clearance_pass else "FAIL",
        delta=(
            "minimum bonus n/a"
            if minimum_bonus is None
            else f"minimum bonus {minimum_bonus:.2%}"
        ),
        delta_color="off",
    )

    if not strict_passes and result.economic_clearance_pass:
        st.warning(
            "Strict instant clearance remains FAIL. Economic clearance is a "
            "conditional PASS only under the selected future-capacity and cost "
            "assumptions; the two tests answer different questions."
        )
    elif not result.economic_clearance_pass:
        st.error(
            "The selected exit route does not recover funding, hedge, and required "
            "return costs within the current liquidation bonus."
        )

    if (
        result.redemption_exit_usd > 0.0
        and result.dex_exit_usd <= max(1.0, extension["sale_usd"] * 1e-9)
    ):
        st.info(
            "The selected route assigns 100% of the sale to primary redemption. "
            "DEX depth therefore does not affect this blended row; the result is "
            "controlled by the assumed redemption throughput, delay, and loss."
        )

    horizon_tab, economics_tab = st.tabs(
        ["Exit horizon", "Liquidator economics"]
    )
    with horizon_tab:
        st.plotly_chart(charts.exit_capacity_fig(extension), width="stretch")
        table = pd.DataFrame(extension["horizon_rows"]).copy()
        for column in (
            "Capacity at horizon",
            "Unresolved sale",
            "Conditional loss",
        ):
            table[column] = table[column].map(_fmt_usd)
        table["Estimated clear time"] = table["Estimated clear time"].map(
            _fmt_duration
        )
        table["Pass by horizon"] = table["Pass by horizon"].map(
            lambda value: "PASS" if value else "FAIL"
        )
        st.dataframe(table, width="stretch", hide_index=True)
        st.caption(
            "Time-to-exit uses the strict ARFC instant batch at the bonus break-even "
            "threshold. Conditional loss marks only the sale still unresolved at the "
            "selected horizon under the unresolved-drawdown assumption."
        )

    with economics_tab:
        chart_columns = st.columns(2)
        with chart_columns[0]:
            st.plotly_chart(
                charts.route_allocation_fig(extension), width="stretch"
            )
        with chart_columns[1]:
            st.plotly_chart(
                charts.economic_clearance_fig(extension), width="stretch"
            )
        rows = []
        for row in extension["economic_rows"]:
            item = row["Result"]
            rows.append(
                {
                    "Regime": row["Regime"],
                    "Exit time": _fmt_duration(item.time_to_clear_hours),
                    "DEX route": _fmt_usd(item.dex_exit_usd),
                    "Redemption route": _fmt_usd(item.redemption_exit_usd),
                    "Profit after hurdle": _fmt_usd(item.economic_profit_usd),
                    "ROI": (
                        "n/a" if item.economic_roi is None else f"{item.economic_roi:.2%}"
                    ),
                    "Minimum bonus": (
                        "n/a"
                        if row["Minimum bonus"] is None
                        else f"{row['Minimum bonus']:.2%}"
                    ),
                    "Economic test": (
                        "PASS" if item.economic_clearance_pass else "FAIL"
                    ),
                }
            )
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        st.caption(
            f"The economic model limits each initial DEX batch to "
            f"{inputs.dex_execution_loss:.2%} execution loss, giving "
            f"{_fmt_usd(extension['economic_instant_capacity_usd'])} of quiet "
            "instant capacity. This is intentionally stricter than the ARFC "
            f"{extension['clearance'].breakeven_slippage:.2%} break-even ceiling."
        )
        st.caption(
            "The warehouse assumes full debt repayment at time zero, sufficient "
            "financing and hedge capacity, deterministic future capacity, and no "
            "reserved redemption slot. Profit maximizing is a static route split, "
            "not an adaptive execution policy."
        )


def _clearance_tab(
    analysis,
    snapshot,
    payload: str,
    base_context: str,
    min_target_share: float,
) -> None:
    clearance = analysis["clearance"]
    if clearance is None:
        st.warning("The active snapshot has no depth calibration.")
        return
    st.subheader("Strict instant clearance")
    quiet_slippage = f"{clearance.slippage_quiet:.2%}"
    if clearance.slippage_quiet_is_lower_bound:
        quiet_slippage = f">= {quiet_slippage}"
    metrics = st.columns(4)
    metrics[0].metric("Largest clearance sale", _fmt_usd(clearance.largest_borrower_usd))
    metrics[1].metric("Max clearable", _fmt_usd(clearance.max_clearable_usd_quiet))
    metrics[2].metric(
        "Quiet slippage lower bound"
        if clearance.slippage_quiet_is_lower_bound
        else "Quiet slippage",
        quiet_slippage,
    )
    metrics[3].metric(
        "Clearable at 50% depth", _fmt_usd(clearance.max_clearable_usd_stressed)
    )
    scope_note = (
        "This account is excluded from the USD debt book and included in the Combined book."
        if clearance.largest_eth_debt_share > 0.5
        else "This account is included in both borrower books."
    )
    st.caption(
        f"Largest account `{clearance.largest_account}`: "
        f"{_fmt_usd(clearance.largest_target_collateral_usd)} target collateral, "
        f"{_fmt_usd(clearance.largest_debt_usd)} debt, and "
        f"{clearance.largest_eth_debt_share:.0%} ETH-denominated debt. {scope_note} "
        "Clearance includes every target-dominant borrower because debt denomination "
        "does not remove its potential collateral sale from the depth test."
    )
    st.caption(
        "Largest clearance sale is the smaller of target collateral and debt plus "
        f"the {snapshot.reserve.liquidation_bonus:.2%} liquidation bonus: "
        f"min({_fmt_usd(clearance.largest_target_collateral_usd)}, "
        f"{_fmt_usd(clearance.largest_debt_usd)} x "
        f"{1.0 + snapshot.reserve.liquidation_bonus:.2f}) = "
        f"{_fmt_usd(clearance.largest_borrower_usd)}. Collateral above that amount "
        "is not seized in this full-liquidation clearance test."
    )
    st.caption(
        "Max clearable is the largest quiet-market sale whose modeled average slippage "
        "does not exceed the liquidator break-even threshold. The 50% depth result "
        "assumes clearable capacity scales linearly with remaining depth."
    )
    st.caption(
        "Splitting the same immediate total across transactions does not improve this "
        "strict test: without replenishment, later sales continue from the depth already "
        "consumed. Splitting helps only across time if liquidity replenishes, the peg or "
        "price recovers, or a liquidator holds or redeems the collateral. Those time-to-exit "
        "channels are outside this strict test and are evaluated separately below."
    )
    if snapshot.depth.source.lower() == "paraswap":
        st.caption(
            "Large Paraswap points may come from price-impact rejection responses. "
            "They are indicative best routes, not guaranteed executable trades; "
            "max clearable interpolates the observed ladder in log-notional space."
        )
    if clearance.slippage_quiet_is_lower_bound:
        st.warning(
            f"The largest sale exceeds the {_fmt_usd(clearance.max_quoted_usd)} quote range. "
            "Its slippage is not extrapolated; the displayed value is the last observed "
            "slippage and therefore only a lower bound."
        )
    if clearance.passes_quiet:
        st.success("ARFC largest-borrower clearance: PASS at quiet depth")
    else:
        st.error("ARFC largest-borrower clearance: FAIL at quiet depth")
    st.plotly_chart(charts.empirical_depth_fig(snapshot, clearance), width="stretch")
    if snapshot.reserve.symbol.lower() == "wsteth":
        st.caption(
            "Strict instant routed depth does not credit the wstETH redemption queue or CEX liquidity."
        )
    _clearance_extension_section(
        snapshot,
        payload,
        base_context,
        min_target_share,
        clearance.passes_quiet,
    )


def _v4_tab(payload: str, context_key: str, scope: str, share: float, n_scen: int, seed: int) -> None:
    state_key = "dashboard_v4_result"
    versioned_context = f"{context_key}|{_V4_MODEL_VERSION}"
    if st.button("Run V3 / V4 comparison", type="primary"):
        with st.spinner("Running matched scenarios across mechanics with ordered clearing..."):
            st.session_state[state_key] = (
                versioned_context,
                _cached_v4_analysis(
                    payload, scope, share, n_scen, seed, _V4_MODEL_VERSION
                ),
            )
    stored = st.session_state.get(state_key)
    if not stored or stored[0] != versioned_context:
        st.info("Run the comparison for the active snapshot and book.")
        return
    rows = stored[1]
    scope_label = "USD-debt" if scope == "USD debt" else scope
    st.markdown(
        f"**Active borrower book:** {scope_label} | {n_scen:,} matched scenarios | ordered clearing"
    )
    if min(row["Positive-loss draws"] for row in rows) < 30:
        st.warning(
            "Fewer than 30 positive-loss draws support at least one row. "
            "The mechanics use common random scenarios, but CVaR levels and "
            "small differences remain low-sample estimates."
        )
    st.plotly_chart(
        charts.mechanics_cvar_fig(rows, scope_label), width="stretch"
    )
    table = pd.DataFrame(rows).copy()
    table["P(bad debt)"] = table["P(bad debt)"].map(lambda value: f"{value:.3%}")
    for column in ("Mean bad debt", "Severity if loss", "VaR99", "CVaR99"):
        table[column] = table[column].map(_fmt_usd)
    table = table.rename(columns={"Mean bad debt": "Expected bad debt"})
    st.dataframe(table, width="stretch", hide_index=True)
    st.caption(
        "This is a mechanics counterfactual, not a measurement of a live V4 market. "
        "The V3 row uses the selected V3 reserve, borrower book, and on-chain risk "
        "parameters. V4 Main is the general-purpose Main Spoke configuration. "
        "V4 Correlated is the configuration for correlated collateral and debt, "
        "such as LST/WETH strategies. Both V4 rows apply those documented Spoke "
        "liquidation configurations to that same V3 book, with scenarios, depth, "
        "liquidation trigger, and ordered execution held fixed."
    )
    st.caption(
        "Repay-to-target and the close-factor floor are separate V4 sizing rules. "
        "The model first computes the repayment needed to restore target HF, then "
        "applies the floor as a minimum repayment fraction. Dynamic bonus and dust "
        "handling also differ from V3. Bonus interpolation between documented "
        "anchors is a modeling assumption."
    )
    st.caption(
        "Expected bad debt is P(bad debt) times average severity conditional on a "
        "loss. CVaR99 averages the worst 1% of all scenarios. When fewer than 1% "
        "of scenarios lose and every loss lies in that tail, CVaR99 equals expected "
        "bad debt divided by 1%, so it is exactly 100 times expected bad debt. This "
        "is a sparse-tail identity, not a V4 scaling factor."
    )


def _episode_tab(payload: str, context_key: str, snapshot, scope: str, share: float) -> None:
    if snapshot.reserve.symbol.lower() != "wsteth":
        st.info("Historical peg replay is available for the wstETH/ETH market.")
        return
    state_key = "dashboard_episode_result"
    if st.button("Run historical replay", type="primary"):
        with st.spinner("Applying committed historical paths to the active book..."):
            st.session_state[state_key] = (
                context_key,
                _cached_episode_analysis(payload, scope, share),
            )
    stored = st.session_state.get(state_key)
    if not stored or stored[0] != context_key:
        st.info("Run the replay for the active snapshot and book.")
        return
    rows = stored[1]
    scope_label = "USD-debt" if scope == "USD debt" else scope
    st.markdown(f"**Active borrower book:** {scope_label}")
    if max(row["Worst bad debt"] for row in rows) > 0:
        st.plotly_chart(
            charts.episode_loss_fig(rows, scope_label), width="stretch"
        )
    else:
        message = "No replayed episode produces bad debt in the active book."
        if scope == "USD debt":
            message += (
                " The June 2022 peg-loss channel belongs to ETH-debt loopers, "
                "which are excluded from this scope."
            )
        st.info(message)
    table = pd.DataFrame(rows).copy()
    table["Worst bad debt"] = table["Worst bad debt"].map(_fmt_usd)
    table["ETH return"] = table["ETH return"].map(
        lambda value: "n/a" if pd.isna(value) else f"{value:+.1%}"
    )
    table["Peg drop"] = table["Peg drop"].map(
        lambda value: "n/a" if pd.isna(value) else f"{value:.2%}"
    )
    st.dataframe(table, width="stretch", hide_index=True)
    st.caption(
        "Historical paths are replayed on the selected snapshot book; this is not archive "
        "backtesting. ETH returns and stETH/ETH peg moves are historical, but "
        "liquidity is not: both rows use the snapshot depth curve, either unchanged or "
        "with an assumed flat 50% haircut because historical routed depth is unavailable. "
        "A 50% haircut can match quiet depth when both sales already stall beyond "
        "the observed quote ladder."
    )


def _multiperiod_tab(
    payload: str,
    base_context: str,
    scope: str,
    share: float,
    default_paths: int,
    seed: int,
) -> None:
    path_controls = st.columns(3)
    periods = int(path_controls[0].number_input("Periods", 1, 24, 8, 1))
    days = float(path_controls[1].number_input("Window (days)", 1.0, 14.0, 4.0, 0.5))
    paths = int(
        path_controls[2].select_slider(
            "Paths", [5_000, 10_000, 20_000], value=min(default_paths, 20_000)
        )
    )
    configured_speed = scenario_config_from_snapshot(
        snapshot_from_json(payload)
    ).stress.peg_mean_reversion_speed
    configured_half_life = (
        math.log(2.0) / configured_speed if configured_speed > 0.0 else 0.0
    )
    stress_controls = st.columns(2)
    replenish = float(
        stress_controls[0].slider("Depth replenishment", 0.0, 1.0, 1.0, 0.1)
    )
    peg_half_life = float(
        stress_controls[1].number_input(
            "Peg residual half-life (days)",
            min_value=0.0,
            max_value=30.0,
            value=min(configured_half_life, 30.0),
            step=0.5,
            help="Zero disables mean reversion and preserves the random-walk baseline.",
        )
    )
    peg_speed = math.log(2.0) / peg_half_life if peg_half_life > 0.0 else 0.0
    context_key = (
        f"{base_context}|{periods}|{days}|{replenish}|{paths}|{peg_speed}|"
        f"{_MULTIPERIOD_MODEL_VERSION}"
    )
    state_key = "dashboard_multiperiod_result"
    if st.button("Run multi-period comparison", type="primary"):
        with st.spinner("Evolving books across stress paths..."):
            st.session_state[state_key] = (
                context_key,
                _cached_multiperiod_analysis(
                    payload,
                    scope,
                    share,
                    paths,
                    seed,
                    periods,
                    days,
                    replenish,
                    peg_speed,
                    _MULTIPERIOD_MODEL_VERSION,
                ),
            )
    stored = st.session_state.get(state_key)
    if not stored or stored[0] != context_key:
        st.info("Run the evolving-path comparison with the selected controls.")
        return
    rows = stored[1]
    scope_label = "USD-debt" if scope == "USD debt" else scope
    st.markdown(
        f"**Active borrower book:** {scope_label} | {paths:,} simulated paths"
    )
    if min(row["Positive-loss paths"] for row in rows) < 30:
        st.warning(
            "Fewer than 30 positive-loss paths support at least one row. "
            "Treat rare-event CVaR differences as low-sample estimates."
        )
    st.plotly_chart(
        charts.multiperiod_cvar_fig(rows, scope_label), width="stretch"
    )
    table = pd.DataFrame(rows).copy()
    table["P(bad debt)"] = table["P(bad debt)"].map(lambda value: f"{value:.3%}")
    table["P(reliquidation)"] = table["P(reliquidation)"].map(
        lambda value: "n/a" if pd.isna(value) else f"{value:.3%}"
    )
    table["Cleared events per path"] = table["Cleared events per path"].map(
        lambda value: "n/a" if pd.isna(value) else f"{value:.4f}"
    )
    for column in ("Mean bad debt", "CVaR99"):
        table[column] = table[column].map(_fmt_usd)
    table = table.rename(columns={"Mean bad debt": "Expected bad debt"})
    st.dataframe(table, width="stretch", hide_index=True)
    st.caption(
        "Both rows share the exact same terminal return, peg drop, and depth "
        "haircut on every matched path. Single-shock marks terminal stalls "
        "immediately. Multi-period follows the route to that endpoint: cleared "
        "positions update debt and collateral, while stalls wait and may recover."
    )
    st.caption(
        "Repeated liquidation does not reset market conditions. Price, peg, and "
        "depth stress evolve cumulatively. A position cleared toward its target HF "
        "can fall below HF 1 again after a later adverse step. Only consumed depth "
        "replenishes according to the selected control; at 100%, consumed capacity "
        "is restored before the next step, but the prevailing depth haircut remains."
    )
    st.caption(
        "Multi-period CVaR is not constrained to be below single-shock CVaR. Early "
        "partial clears pay liquidation bonuses, reduce collateral, and consume depth. "
        "With partial replenishment, those costs can outweigh deleveraging if the "
        "restored HF buffer is small; a higher target HF can reverse that result."
    )
    st.caption(
        "At each period, peg drop equals the clipped sum of base peg stress, crash "
        "beta times cumulative ETH drawdown, and an idiosyncratic OU residual. "
        + (
            f"The selected residual half-life is {peg_half_life:g} days."
            if peg_half_life > 0.0
            else "Mean reversion is disabled, so the residual is a random walk."
        )
        + " The peg is never reset mechanically between periods."
    )


def _data_tab(snapshot, payload: str, source: str) -> None:
    reserve = snapshot.reserve
    reserve_table = pd.DataFrame(
        [
            {
                "Source": source,
                "Chain": snapshot.chain,
                "Asset": reserve.symbol,
                "Block": f"{snapshot.block:,}",
                "Date": _snapshot_date(snapshot),
                "Oracle price": _fmt_usd(reserve.price_usd),
                "Supply cap": f"{reserve.supply_cap_tokens:,.0f}",
                "Supplied": f"{reserve.total_supplied_tokens:,.0f}",
                "Borrow cap": f"{reserve.borrow_cap_tokens:,.0f}",
                "Borrowed": f"{reserve.total_debt_tokens:,.0f}",
                "LT": f"{reserve.liquidation_threshold:.2%}",
                "Bonus": f"{reserve.liquidation_bonus:.2%}",
            }
        ]
    )
    st.dataframe(reserve_table, width="stretch", hide_index=True)
    source_rows = []
    if snapshot.stress is not None:
        source_rows.append({"Input": "Stress calibration", "Source": snapshot.stress.source})
    if snapshot.depth is not None:
        source_rows.append(
            {
                "Input": "Depth calibration",
                "Source": f"{snapshot.depth.source} {snapshot.depth.pair}",
            }
        )
    source_rows.append(
        {
            "Input": "Borrower discovery",
            "Source": snapshot.notes or "Legacy snapshot without discovery metadata",
        }
    )
    discovery = snapshot.borrower_discovery
    if discovery is not None:
        source_rows.extend(
            [
                {
                    "Input": "Borrower registry coverage",
                    "Source": (
                        f"blocks {discovery.from_block:,} to {discovery.to_block:,}; "
                        f"{discovery.candidate_count:,} historical addresses"
                    ),
                },
                {
                    "Input": "Active account filter",
                    "Source": (
                        f"{discovery.active_count:,} accounts with debt >= "
                        f"{_fmt_usd(discovery.min_debt_usd)}"
                    ),
                },
            ]
        )
    st.dataframe(pd.DataFrame(source_rows), width="stretch", hide_index=True)
    st.download_button(
        "Download snapshot JSON",
        data=payload,
        file_name=f"aave_v3_{snapshot.chain}_{reserve.symbol.lower()}_block_{snapshot.block}.json",
        mime="application/json",
    )
    st.caption(
        "The complete registry retains dormant borrowers after their first Borrow event. "
        "Real books still use an effective single-asset mapping."
    )


def main() -> None:
    st.set_page_config(page_title="Aave Market Risk", layout="wide")
    st.logo(_LOGO_PATH, size="large", icon_image=_LOGO_PATH)
    st.markdown(
        """
        <style>
        .block-container {padding-top: 1.4rem; padding-bottom: 2rem; max-width: 1480px;}
        [data-testid="stMetricValue"] {font-size: 1.55rem;}
        [data-testid="stMetricLabel"] {font-size: 0.86rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.sidebar.title("Market controls")
    market_name = st.sidebar.selectbox("Real market", list(_MARKETS))
    market = _MARKETS[market_name]
    scope = st.sidebar.selectbox("Borrower book", ["USD debt", "Combined"])
    share = float(st.sidebar.slider("Minimum target share", 0.5, 0.9, 0.5, 0.1))
    scenarios = int(
        st.sidebar.select_slider(
            "Scenarios",
            [5_000, 10_000, 20_000, 40_000, 100_000, 1_000_000],
            value=20_000,
        )
    )
    seed = int(st.sidebar.number_input("Seed", 0, 9_999, 7, 1))
    budget = float(
        st.sidebar.number_input(
            "CVaR99 budget ($)",
            min_value=100_000.0,
            max_value=100_000_000.0,
            value=market["budget"],
            step=100_000.0,
            key=f"budget::{market_name}",
        )
    )
    snapshot, payload = _active_snapshot(market_name, market)
    source = st.session_state[f"snapshot_source::{market_name}"]

    st.title("Aave Market Risk")
    st.caption(
        f"{market_name} | {source.lower()} snapshot | block {snapshot.block:,} | "
        f"{_snapshot_date(snapshot)} | {scope.lower()} book"
    )
    try:
        with st.spinner("Running current-book risk analysis..."):
            analysis = _cached_market_analysis(
                payload,
                scope,
                share,
                scenarios,
                seed,
                budget,
                True,
            )
    except Exception as exc:  # noqa: BLE001 - render snapshot controls after invalid live data
        st.error(f"The active snapshot cannot be analyzed: {exc}")
        return

    snapshot_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    context = f"{snapshot_id}|{scope}|{share}|{scenarios}|{seed}|Ordered"
    tabs = st.tabs(
        [
            "Overview",
            "Sensitivities",
            "Clearance",
            "V3 / V4",
            "Episodes",
            "Multi-period",
            "Data",
        ]
    )
    with tabs[0]:
        _overview_tab(analysis, snapshot, share, budget, scope)
    with tabs[1]:
        _sensitivities_tab(
            analysis,
            snapshot,
            payload,
            context,
            scope,
            share,
            scenarios,
            seed,
            True,
        )
    with tabs[2]:
        _clearance_tab(analysis, snapshot, payload, context, share)
    with tabs[3]:
        _v4_tab(payload, context, scope, share, scenarios, seed)
    with tabs[4]:
        _episode_tab(payload, context, snapshot, scope, share)
    with tabs[5]:
        _multiperiod_tab(payload, context, scope, share, scenarios, seed)
    with tabs[6]:
        _data_tab(snapshot, payload, source)


if __name__ == "__main__":
    main()
