"""Interactive real-market dashboard for the Aave risk engine."""

from __future__ import annotations

import datetime as dt
import hashlib
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
    account_rows,
    run_episode_analysis,
    run_liquidation_threshold_sensitivity,
    run_market_analysis,
    run_multiperiod_analysis,
    run_v4_analysis,
)
from aave_risk_engine.data import load_snapshot, snapshot_from_json, snapshot_to_json


_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_LOGO_PATH = os.path.join(_PACKAGE_DIR, "aave_logo.png")
_RUNTIME_DIR = os.path.join(_PACKAGE_DIR, ".runtime")
_MARKETS = {
    "Ethereum wstETH": {
        "path": os.path.join(_PACKAGE_DIR, "data", "snapshots", "aave_v3_ethereum_wsteth.json"),
        "chain": "ethereum",
        "asset": "wstETH",
        "pair_dest": "WETH",
        "blocks": 100_000,
        "ladder_usd": (25e3, 100e3, 500e3, 2e6, 8e6, 25e6),
        "budget": 5_000_000.0,
    },
    "Linea WETH": {
        "path": os.path.join(_PACKAGE_DIR, "data", "snapshots", "aave_v3_linea_weth.json"),
        "chain": "linea",
        "asset": "WETH",
        "pair_dest": "USDC",
        "blocks": 1_200_000,
        "ladder_usd": (5e3, 15e3, 41e3, 100e3, 300e3, 1e6),
        "budget": 500_000.0,
    },
}
_LIVE_REFRESH_TIMEOUT_S = 360


def _fmt_usd(value: float) -> str:
    if not np.isfinite(value):
        return "n/a"
    for unit, divisor in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(value) >= divisor:
            return f"${value / divisor:,.2f}{unit}"
    return f"${value:,.0f}"


def _snapshot_date(snapshot) -> str:
    return dt.datetime.fromtimestamp(snapshot.timestamp, dt.timezone.utc).date().isoformat()


def _refresh_snapshot(market: dict):
    """Build a live snapshot in a bounded child process."""
    parent_dir = os.path.dirname(_PACKAGE_DIR)
    os.makedirs(_RUNTIME_DIR, exist_ok=True)
    output_path = os.path.join(_RUNTIME_DIR, f"snapshot_{uuid.uuid4().hex}.json")
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
            "--blocks",
            str(market["blocks"]),
            "--ladder-usd",
            ",".join(str(value) for value in market["ladder_usd"]),
            "--rpc-timeout",
            "12",
            "--rpc-retries",
            "1",
            "--no-borrower-cache",
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
):
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
):
    return run_multiperiod_analysis(
        snapshot_from_json(payload),
        scope,
        min_target_share,
        n_paths,
        seed,
        n_periods,
        total_days,
        replenish,
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
        st.caption("Live refresh is an explicit network action and may take up to six minutes.")
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
    second = st.columns(3)
    second[0].metric("P(bad debt)", f"{result.prob_bad_debt:.3%}")
    second[1].metric("CVaR99", _fmt_usd(result.cvar))
    sweep = analysis["recommendation"]["sweep"]
    reaches_sweep_limit = bool(
        np.isclose(recommendation, sweep["caps"][-1])
        and sweep["cvar"][-1] <= analysis["recommendation"]["budget"]
    )
    second[2].metric(
        "Safe within sweep" if reaches_sweep_limit else "Model-safe exposure",
        _fmt_usd(recommendation),
    )
    loss_count = int(np.count_nonzero(result.bad_debt > 0))
    third = st.columns(4)
    third[0].metric("Accounts", f"{book.debt_usd.size:,}")
    third[1].metric("Median health factor", f"{float(np.median(book.hf0)):.2f}")
    third[2].metric("Positive-loss draws", f"{loss_count:,} / {result.bad_debt.size:,}")
    third[3].metric(
        "ETH-denominated debt" if scope == "Combined" else "ETH-debt loopers",
        _fmt_usd(eth_debt) if scope == "Combined" else "Excluded",
    )


def _overview_tab(
    analysis,
    snapshot,
    min_target_share: float,
    budget_usd: float,
    scope: str,
) -> None:
    _display_metrics(analysis, snapshot, scope)
    loss_count = int(np.count_nonzero(analysis["result"].bad_debt > 0))
    if loss_count < 30:
        st.warning(
            f"Only {loss_count:,} positive-loss draws were observed. "
            "Treat rare-event probability and CVaR as statistically noisy; "
            "increase Scenarios and compare seeds before publishing."
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
            "This is an immediate current-book transition test. Each account's "
            "weighted LT moves with its target collateral share; borrower deleveraging "
            "and future origination responses are not modeled."
        )


def _clearance_tab(analysis, snapshot) -> None:
    clearance = analysis["clearance"]
    if clearance is None:
        st.warning("The active snapshot has no depth calibration.")
        return
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
        "Max clearable is the largest quiet-market sale whose modeled average slippage "
        "does not exceed the liquidator break-even threshold. The 50% depth result "
        "assumes clearable capacity scales linearly with remaining depth."
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


def _v4_tab(payload: str, context_key: str, scope: str, share: float, n_scen: int, seed: int) -> None:
    state_key = "dashboard_v4_result"
    if st.button("Run V3 / V4 comparison", type="primary"):
        with st.spinner("Running matched scenarios across mechanics and queue modes..."):
            st.session_state[state_key] = (
                context_key,
                _cached_v4_analysis(payload, scope, share, n_scen, seed),
            )
    stored = st.session_state.get(state_key)
    if not stored or stored[0] != context_key:
        st.info("Run the comparison for the active snapshot and book.")
        return
    rows = stored[1]
    st.plotly_chart(charts.mechanics_cvar_fig(rows), width="stretch")
    table = pd.DataFrame(rows).copy()
    table["P(bad debt)"] = table["P(bad debt)"].map(lambda value: f"{value:.2%}")
    for column in ("Mean bad debt", "VaR99", "CVaR99"):
        table[column] = table[column].map(_fmt_usd)
    st.dataframe(table, width="stretch", hide_index=True)
    st.caption(
        "V4 bonus interpolation between documented anchors is a modeling assumption."
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
    st.plotly_chart(charts.episode_loss_fig(rows), width="stretch")
    table = pd.DataFrame(rows).copy()
    table["Worst bad debt"] = table["Worst bad debt"].map(_fmt_usd)
    table["ETH return"] = table["ETH return"].map(lambda value: f"{value:+.1%}")
    table["Peg drop"] = table["Peg drop"].map(lambda value: f"{value:.2%}")
    st.dataframe(table, width="stretch", hide_index=True)
    st.caption("Historical paths are replayed on today's book; this is not archive backtesting.")


def _multiperiod_tab(
    payload: str,
    base_context: str,
    scope: str,
    share: float,
    default_paths: int,
    seed: int,
) -> None:
    controls = st.columns(4)
    periods = int(controls[0].number_input("Periods", 1, 24, 8, 1))
    days = float(controls[1].number_input("Window (days)", 1.0, 14.0, 4.0, 0.5))
    replenish = float(controls[2].slider("Depth replenishment", 0.0, 1.0, 1.0, 0.1))
    paths = int(
        controls[3].select_slider(
            "Paths", [5_000, 10_000, 20_000], value=min(default_paths, 20_000)
        )
    )
    context_key = f"{base_context}|{periods}|{days}|{replenish}|{paths}"
    state_key = "dashboard_multiperiod_result"
    if st.button("Run multi-period comparison", type="primary"):
        with st.spinner("Evolving books across stress paths..."):
            st.session_state[state_key] = (
                context_key,
                _cached_multiperiod_analysis(
                    payload, scope, share, paths, seed, periods, days, replenish
                ),
            )
    stored = st.session_state.get(state_key)
    if not stored or stored[0] != context_key:
        st.info("Run the evolving-path comparison with the selected controls.")
        return
    rows = stored[1]
    st.plotly_chart(charts.multiperiod_cvar_fig(rows), width="stretch")
    table = pd.DataFrame(rows).copy()
    table["P(bad debt)"] = table["P(bad debt)"].map(lambda value: f"{value:.2%}")
    table["P(reliquidation)"] = table["P(reliquidation)"].map(lambda value: f"{value:.2%}")
    table["Events per path"] = table["Events per path"].map(lambda value: f"{value:.2f}")
    for column in ("Mean bad debt", "CVaR99"):
        table[column] = table[column].map(_fmt_usd)
    st.dataframe(table, width="stretch", hide_index=True)


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
            "Source": f"Borrow events over {snapshot.scan_blocks or 0:,} blocks",
        }
    )
    st.dataframe(pd.DataFrame(source_rows), width="stretch", hide_index=True)
    st.download_button(
        "Download snapshot JSON",
        data=payload,
        file_name=f"aave_v3_{snapshot.chain}_{reserve.symbol.lower()}_block_{snapshot.block}.json",
        mime="application/json",
    )
    st.caption(
        "Dormant borrowers outside the scan window may be missed. Real books use an effective single-asset mapping."
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
    queue = st.sidebar.selectbox("Queue clearing", ["Aggregate", "Ordered"])
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
                queue == "Ordered",
            )
    except Exception as exc:  # noqa: BLE001 - render snapshot controls after invalid live data
        st.error(f"The active snapshot cannot be analyzed: {exc}")
        return

    snapshot_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    context = f"{snapshot_id}|{scope}|{share}|{scenarios}|{seed}|{queue}"
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
            queue == "Ordered",
        )
    with tabs[2]:
        _clearance_tab(analysis, snapshot)
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
