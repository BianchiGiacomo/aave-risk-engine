"""Interactive real-market dashboard for the Aave risk engine."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import streamlit as st

from aave_risk_engine import dashboard_charts as charts
from aave_risk_engine.dashboard_analysis import (
    account_rows,
    run_episode_analysis,
    run_market_analysis,
    run_multiperiod_analysis,
    run_v4_analysis,
)
from aave_risk_engine.data import load_snapshot, snapshot_from_json, snapshot_to_json


_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_LOGO_PATH = os.path.join(_PACKAGE_DIR, "aave_logo.png")
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
    with tempfile.TemporaryDirectory(prefix="aave_snapshot_") as temp_dir:
        output_path = os.path.join(temp_dir, "snapshot.json")
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
    first[0].metric("On-chain supplied", _fmt_usd(reserve.supplied_usd))
    first[1].metric("Supply cap usage", f"{cap_usage:.0%}" if np.isfinite(cap_usage) else "n/a")
    first[2].metric("Book exposure", _fmt_usd(book.total_debt))
    second = st.columns(3)
    second[0].metric("P(bad debt)", f"{result.prob_bad_debt:.2%}")
    second[1].metric("CVaR99", _fmt_usd(result.cvar))
    second[2].metric("Model-safe exposure", _fmt_usd(recommendation))
    third = st.columns(3)
    third[0].metric("Accounts", f"{book.debt_usd.size:,}")
    third[1].metric("Median health factor", f"{float(np.median(book.hf0)):.2f}")
    third[2].metric(
        "ETH-denominated debt" if scope == "Combined" else "ETH-debt loopers",
        _fmt_usd(eth_debt) if scope == "Combined" else "Excluded",
    )


def _overview_tab(analysis, snapshot, min_target_share: float, budget_usd: float) -> None:
    _display_metrics(analysis, snapshot, "Combined" if analysis["book"].eth_debt_usd is not None else "USD debt")
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
    if rows:
        st.plotly_chart(charts.concentration_fig(rows), width="stretch")
        top = pd.DataFrame(rows[:10]).copy()
        top["Debt"] = top["Debt"].map(_fmt_usd)
        top["Collateral"] = top["Collateral"].map(_fmt_usd)
        top["Target share"] = top["Target share"].map(lambda value: f"{value:.0%}")
        top["ETH debt share"] = top["ETH debt share"].map(lambda value: f"{value:.0%}")
        top["Health factor"] = top["Health factor"].map(lambda value: f"{value:.2f}")
        st.dataframe(top, width="stretch", hide_index=True)


def _clearance_tab(analysis, snapshot) -> None:
    clearance = analysis["clearance"]
    if clearance is None:
        st.warning("The active snapshot has no depth calibration.")
        return
    metrics = st.columns(4)
    metrics[0].metric("Largest borrower sale", _fmt_usd(clearance.largest_borrower_usd))
    metrics[1].metric("Max clearable", _fmt_usd(clearance.max_clearable_usd_quiet))
    metrics[2].metric("Quiet slippage", f"{clearance.slippage_quiet:.2%}")
    metrics[3].metric("50% haircut clearable", _fmt_usd(clearance.max_clearable_usd_stressed))
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
        st.sidebar.select_slider("Scenarios", [5_000, 10_000, 20_000, 40_000], value=20_000)
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
    tabs = st.tabs(["Overview", "Clearance", "V3 / V4", "Episodes", "Multi-period", "Data"])
    with tabs[0]:
        _overview_tab(analysis, snapshot, share, budget)
    with tabs[1]:
        _clearance_tab(analysis, snapshot)
    with tabs[2]:
        _v4_tab(payload, context, scope, share, scenarios, seed)
    with tabs[3]:
        _episode_tab(payload, context, snapshot, scope, share)
    with tabs[4]:
        _multiperiod_tab(payload, context, scope, share, scenarios, seed)
    with tabs[5]:
        _data_tab(snapshot, payload, source)


if __name__ == "__main__":
    main()
