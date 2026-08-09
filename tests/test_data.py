"""Offline tests for the market-data layer (no network access needed)."""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace

import numpy as np

from aave_risk_engine.config import RiskParams
from aave_risk_engine.dashboard_charts import (
    empirical_depth_fig,
    episode_loss_fig,
    loss_distribution_fig,
    mechanics_cvar_fig,
    multiperiod_cvar_fig,
    scenario_scatter_fig,
    slippage_curve_fig,
)
from aave_risk_engine.dashboard_analysis import (
    account_rows,
    run_episode_analysis,
    run_liquidation_threshold_sensitivity,
    run_multiperiod_analysis,
    run_v4_analysis,
)
from aave_risk_engine.data.aave_v3 import _addr_arg, _word_to_address, _words
from aave_risk_engine.data.book import build_real_book, scenario_config_from_snapshot
from aave_risk_engine.data.clearance import arfc_clearance_test, max_clearable_notional
from aave_risk_engine.data.depth import fit_depth, quotes_to_slippage_points
from aave_risk_engine.data.markets import (
    arfc_peg_check,
    fit_student_t_dof,
    realized_annual_vol,
)
from aave_risk_engine.data.snapshot import (
    AccountRecord,
    DepthCalibration,
    MarketSnapshot,
    ReserveState,
    StressCalibration,
    load_snapshot,
    save_snapshot,
    snapshot_from_json,
    snapshot_to_json,
)
from aave_risk_engine.engine import RiskResult
from aave_risk_engine.liquidation import process_chunk
from aave_risk_engine.positions import PositionBook, scale_book
from aave_risk_engine.slippage import (
    calibrate_liquidity,
    empirical_slippage,
    max_notional_within,
    slippage,
)


def _reserve(**overrides) -> ReserveState:
    fields = dict(
        symbol="wstETH",
        address="0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0",
        decimals=18,
        price_usd=2_000.0,
        ltv=0.785,
        liquidation_threshold=0.81,
        liquidation_bonus=0.06,
        reserve_factor=0.35,
        borrow_cap_tokens=1.0,
        supply_cap_tokens=1_000_000.0,
        total_supplied_tokens=840_000.0,
        total_debt_tokens=2_500.0,
    )
    fields.update(overrides)
    return ReserveState(**fields)


def _snapshot() -> MarketSnapshot:
    accounts = [
        AccountRecord("0xaa", 40_000_000.0, 20_000_000.0, 0.80, 1.60, 38_000_000.0),
        AccountRecord("0xbb", 10_000_000.0, 6_000_000.0, 0.78, 1.30, 9_000_000.0),
        AccountRecord("0xcc", 5_000_000.0, 3_000_000.0, 0.82, 1.37, 1_000_000.0),  # low share
        AccountRecord("0xdd", 100_000.0, 5_000.0, 0.80, 16.0, 100_000.0),  # tiny debt
        AccountRecord("0xee", 2_000_000.0, 1_500_000.0, 0.75, 1.00, 2_000_000.0),
        # WETH-debt looper: debt leg falls with collateral in a USD crash.
        AccountRecord("0xff", 20_000_000.0, 10_000_000.0, 0.80, 1.60, 20_000_000.0, 9_000_000.0),
    ]
    depth = DepthCalibration(
        source="test",
        pair="wstETH/WETH",
        quoted_at=0,
        points=[[10_000_000.0, 0.008]],
        liquidity=calibrate_liquidity(2_000.0, 25_000_000.0, 0.02),
        ref_notional_usd=25_000_000.0,
        ref_slippage=0.02,
    )
    stress = StressCalibration(
        source="test", lookback_days=365, annual_vol=0.60, t_dof=4.0,
        peg_pass=True, peg_worst_deviation=0.006, peg_max_run_days=0.0,
    )
    return MarketSnapshot(
        chain="ethereum", block=25_000_000, timestamp=1_784_000_000,
        reserve=_reserve(), accounts=accounts, depth=depth, stress=stress,
    )


def test_abi_word_decoding():
    assert _addr_arg("0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0") == (
        "0000000000000000000000007f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"
    )
    packed = "0x" + "12".rjust(64, "0") + "ff".rjust(64, "0")
    assert _words(packed) == [0x12, 0xFF]
    assert _word_to_address(0x7F39C581F595B53C5CB19BD0B3F8DA6C935E2CA0) == (
        "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"
    )


def test_snapshot_roundtrip():
    snap = _snapshot()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sub", "snap.json")
        save_snapshot(snap, path)
        loaded = load_snapshot(path)
    assert loaded.reserve == snap.reserve
    assert loaded.accounts == snap.accounts
    assert loaded.depth == snap.depth
    assert loaded.stress == snap.stress


def test_snapshot_json_payload_roundtrip():
    snap = _snapshot()
    payload = snapshot_to_json(snap)
    loaded = snapshot_from_json(payload.encode("utf-8"))
    assert loaded.block == snap.block
    assert loaded.reserve.symbol == "wstETH"
    assert loaded.accounts[0].address == "0xaa"


def test_build_real_book_filters_and_per_position_lt():
    book = build_real_book(_snapshot(), min_target_share=0.5, min_debt_usd=10_000.0)
    # 0xcc fails the share filter, 0xdd the debt floor, 0xff the ETH-debt cap.
    assert book.debt_usd.size == 3
    assert book.lt is not None and book.lt.size == 3
    # Sorted by debt: 0xaa, 0xbb, 0xee.
    assert np.isclose(book.debt_usd[0], 20_000_000.0)
    assert np.isclose(book.lt[0], 0.80)
    # HF reconstruction matches on-chain HF: coll * lt / debt.
    assert np.isclose(book.hf0[0], 40_000_000.0 * 0.80 / 20_000_000.0)
    # Allowing ETH-denominated debt readmits the looper.
    loose = build_real_book(_snapshot(), max_eth_debt_share=1.0)
    assert loose.debt_usd.size == 4


def test_dashboard_account_rows_match_selected_book():
    combined_rows = account_rows(_snapshot(), min_target_share=0.5)
    usd_rows = [row for row in combined_rows if row["ETH debt share"] <= 0.5]
    assert [row["Account"] for row in usd_rows] == ["0xaa", "0xbb", "0xee"]
    assert [row["Account"] for row in combined_rows] == ["0xaa", "0xff", "0xbb", "0xee"]


def test_dashboard_loss_chart_scales_single_small_loss():
    losses = np.zeros(20_000)
    losses[123] = 8_689.56
    result = RiskResult(
        bad_debt=losses,
        slippage=np.zeros_like(losses),
        liquidated_usd=np.zeros_like(losses),
        frac_liquidated=np.zeros_like(losses),
        total_debt=83_620.0,
        cvar_level=0.99,
    )
    figure = loss_distribution_fig(result)
    assert figure.layout.xaxis.title.text == "bad debt ($k)"
    assert figure.data[0].type == "scatter"
    assert np.isclose(figure.data[0].x[0], 8.68956)


def test_dashboard_sensitivity_charts_use_real_scenarios_and_depth():
    from aave_risk_engine.config import SimConfig
    from aave_risk_engine.engine import RiskEngine

    snap = _snapshot()
    snap.depth.points = [
        [120_000.0, 0.0001], [2_330_000.0, 0.0034], [9_340_000.0, 0.5455]
    ]
    cfg = scenario_config_from_snapshot(snap)
    cfg.sim = SimConfig(n_scenarios=2_000, seed=7, chunk_size=1_000)
    engine = RiskEngine(cfg)
    result = engine.run(book=build_real_book(snap))

    slippage_figure = slippage_curve_fig(engine, stress_haircut=0.5)
    assert slippage_figure.layout.xaxis.type == "log"
    assert slippage_figure.layout.xaxis.tickmode == "array"
    assert "modeled stress haircut" in slippage_figure.layout.title.text
    assert [trace.name for trace in slippage_figure.data] == [
        "quiet depth",
        "50% depth haircut",
    ]
    assert np.allclose(
        np.asarray(slippage_figure.data[1].x),
        np.asarray(slippage_figure.data[0].x) * 0.5,
    )

    scenario_figure = scenario_scatter_fig(engine, result)
    assert "Scenario attribution" in scenario_figure.layout.title.text
    assert any("peg drop" in trace.hovertemplate for trace in scenario_figure.data)


def test_dashboard_advanced_tabs_expose_scope_and_sparse_event_counts():
    snap = _snapshot()
    v4_rows = run_v4_analysis(snap, "USD debt", 0.5, 2_000, 7)
    assert len(v4_rows) == 6
    assert all("Positive-loss draws" in row for row in v4_rows)
    assert "USD-debt book" in mechanics_cvar_fig(
        v4_rows, "USD-debt"
    ).layout.title.text

    multi_rows = run_multiperiod_analysis(
        snap,
        "USD debt",
        0.5,
        n_paths=200,
        seed=7,
        n_periods=2,
        total_days=2.0,
        replenish=1.0,
    )
    single_rows = [row for row in multi_rows if row["Path"] == "Single-shock"]
    evolving_rows = [row for row in multi_rows if row["Path"] == "Multi-period"]
    assert all(row["P(reliquidation)"] is None for row in single_rows)
    assert all(row["Cleared events per path"] is None for row in single_rows)
    assert all("Positive-loss paths" in row for row in evolving_rows)
    assert "USD-debt book" in multiperiod_cvar_fig(
        multi_rows, "USD-debt"
    ).layout.title.text


def test_zero_loss_episode_rows_do_not_report_arbitrary_market_drivers():
    snap = load_snapshot()
    usd_rows = run_episode_analysis(snap, "USD debt", 0.5)
    assert all(row["Worst bad debt"] == 0.0 for row in usd_rows)
    assert all(row["Window"] == "n/a" for row in usd_rows)
    assert all(row["ETH return"] is None for row in usd_rows)
    assert all(row["Peg drop"] is None for row in usd_rows)
    assert "USD-debt book" in episode_loss_fig(
        usd_rows, "USD-debt"
    ).layout.title.text

    combined_rows = run_episode_analysis(snap, "Combined", 0.5)
    loss_rows = [row for row in combined_rows if row["Worst bad debt"] > 0]
    assert loss_rows
    assert all(row["Window"] != "n/a" for row in loss_rows)
    assert all(row["ETH return"] is not None for row in loss_rows)
    assert all(row["Peg drop"] is not None for row in loss_rows)


def test_market_report_manifest_records_snapshot_and_tail_diagnostics():
    from aave_risk_engine.config import SimConfig
    from aave_risk_engine.data.clearance import arfc_clearance_test
    from aave_risk_engine.engine import RiskEngine
    from aave_risk_engine.run_market_report import _write_manifest

    snap = _snapshot()
    cfg = scenario_config_from_snapshot(snap)
    cfg.sim = SimConfig(n_scenarios=2_000, seed=7, chunk_size=1_000)
    book = build_real_book(snap)
    engine = RiskEngine(cfg)
    result = engine.run(book=book)
    recommendation = engine.recommend_cap(
        5e6,
        cap_min=book.total_debt * 0.5,
        cap_max=book.total_debt * 1.5,
        n_grid=3,
        book=book,
    )
    args = SimpleNamespace(
        n_scenarios=2_000,
        seed=7,
        budget=5e6,
        min_target_share=0.5,
        ordered=False,
    )
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = os.path.join(tmp, "snapshot.json")
        manifest_path = os.path.join(tmp, "manifest.json")
        save_snapshot(snap, snapshot_path)
        _write_manifest(
            manifest_path,
            snapshot_path,
            snap,
            cfg,
            args,
            book,
            result,
            None,
            None,
            [],
            recommendation,
            arfc_clearance_test(snap),
        )
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)

    assert manifest["snapshot"]["block"] == snap.block
    assert len(manifest["snapshot"]["sha256"]) == 64
    metrics = manifest["books"]["usd_debt"]["metrics"]
    assert metrics["positive_loss_draws"] == result.positive_loss_count
    assert metrics["prob_bad_debt_ci95"][0] <= result.prob_bad_debt


def test_real_book_liquidation_threshold_transition_sensitivity():
    from aave_risk_engine.config import SimConfig
    from aave_risk_engine.engine import RiskEngine

    snap = _snapshot()
    sweep = run_liquidation_threshold_sensitivity(
        snap,
        scope="USD debt",
        min_target_share=0.5,
        n_scenarios=2_000,
        seed=7,
        ordered=False,
        n_grid=5,
    )
    assert sweep["thresholds"].size == 5
    assert np.any(np.isclose(sweep["thresholds"], 0.81))
    assert sweep["cvar"].shape == sweep["thresholds"].shape
    assert sweep["prob"].shape == sweep["thresholds"].shape
    assert sweep["underwater_accounts"].shape == sweep["thresholds"].shape
    assert sweep["prob"][0] >= sweep["prob"][-1]
    current_index = int(
        np.flatnonzero(np.isclose(sweep["thresholds"], sweep["current_threshold"]))[0]
    )
    cfg = scenario_config_from_snapshot(snap)
    cfg.sim = SimConfig(n_scenarios=2_000, seed=7, chunk_size=2_000)
    baseline = RiskEngine(cfg).run(book=build_real_book(snap))
    assert np.isclose(sweep["cvar"][current_index], baseline.cvar)
    assert np.isclose(sweep["prob"][current_index], baseline.prob_bad_debt)


def test_scale_book_preserves_health_factors():
    book = build_real_book(_snapshot())
    scaled = scale_book(book, book.total_debt * 2.5)
    assert np.isclose(scaled.total_debt, book.total_debt * 2.5)
    assert np.allclose(
        scaled.coll_units * 2_000.0 * scaled.lt / scaled.debt_usd,
        book.coll_units * 2_000.0 * book.lt / book.debt_usd,
    )


def test_per_position_lt_changes_liquidation_outcome():
    risk = RiskParams(ltv=0.70, liquidation_threshold=0.80, liquidation_bonus=0.05)
    debt = np.array([80.0, 80.0])
    units = np.array([1.0, 1.0])
    price = np.array([95.0])
    depth = np.array([1e12])
    # Same debt and collateral; only the per-position LT differs.
    tight = PositionBook(debt, units, np.ones(2), lt=np.array([0.80, 0.90]))
    res = process_chunk(tight, price, depth, risk, delay_drawdown=0.0)
    # HF: 95*0.80/80 = 0.95 (liquidatable) vs 95*0.90/80 = 1.07 (safe).
    assert res.liquidated_usd[0] > 0
    only_first = process_chunk(
        PositionBook(debt[:1], units[:1], np.ones(1), lt=np.array([0.80])),
        price, depth, risk, delay_drawdown=0.0,
    )
    assert np.isclose(res.liquidated_usd[0], only_first.liquidated_usd[0])


def test_scenario_config_from_snapshot_wires_calibration():
    cfg = scenario_config_from_snapshot(_snapshot())
    assert cfg.asset.name == "wstETH"
    assert np.isclose(cfg.asset.spot_price, 2_000.0)
    assert np.isclose(cfg.risk.liquidation_threshold, 0.81)
    assert np.isclose(cfg.risk.liquidation_bonus, 0.06)
    assert np.isclose(cfg.liquidity.ref_notional_usd, 25_000_000.0)
    assert np.isclose(cfg.stress.eth_annual_vol, 0.60)
    assert np.isclose(cfg.stress.tail_dof, 4.0)


def test_depth_fit_recovers_known_liquidity():
    price = 2_000.0
    true_l = calibrate_liquidity(price, 30_000_000.0, 0.025)
    sizes_usd = np.array([2e6, 8e6, 20e6, 45e6])
    slips = slippage(sizes_usd, true_l, price)
    fitted = fit_depth(
        [[float(n), float(s)] for n, s in zip(sizes_usd, slips)],
        price, source="test", pair="X/Y",
    )
    assert np.isclose(fitted.liquidity, true_l, rtol=1e-9)
    round_trip = calibrate_liquidity(price, fitted.ref_notional_usd, fitted.ref_slippage)
    assert np.isclose(round_trip, true_l, rtol=1e-9)


def test_quotes_to_slippage_points_uses_small_size_mid():
    quotes = [(10.0, 12.0), (1_000.0, 1_188.0), (100.0, 119.4)]
    points = quotes_to_slippage_points(quotes, price_usd=2_000.0)
    assert len(points) == 2
    assert np.isclose(points[0][0], 100.0 * 2_000.0)
    assert np.isclose(points[0][1], 1.0 - (119.4 / 100.0) / 1.2)
    assert np.isclose(points[1][1], 1.0 - (1_188.0 / 1_000.0) / 1.2)


def test_arfc_peg_check_windows():
    clean = np.full(100, 0.999)
    assert arfc_peg_check(clean)["peg_pass"]
    one_day = np.full(100, 1.0)
    one_day[50] = 0.98
    assert arfc_peg_check(one_day)["peg_pass"]
    sustained = np.full(100, 1.0)
    sustained[50:53] = 0.985
    out = arfc_peg_check(sustained)
    assert not out["peg_pass"]
    assert np.isclose(out["peg_max_run_days"], 3.0)
    assert np.isclose(out["peg_worst_deviation"], 0.015)


def test_vol_and_dof_estimators():
    rng = np.random.default_rng(0)
    n = 4_000
    sigma_daily = 0.60 / np.sqrt(365.0)
    gauss = np.exp(np.cumsum(sigma_daily * rng.standard_normal(n)))
    vol = realized_annual_vol(gauss)
    assert 0.5 < vol < 0.7
    assert fit_student_t_dof(gauss) is None
    t_rets = sigma_daily * rng.standard_t(5.0, size=n)
    heavy = np.exp(np.cumsum(t_rets))
    dof = fit_student_t_dof(heavy)
    assert dof is not None and 2.6 <= dof <= 12.0


def test_empirical_slippage_interpolation_and_inversion():
    # A realistic cliff: cheap to $2m, catastrophic past $9m.
    points = [[120_000.0, 0.0001], [2_330_000.0, 0.0034], [9_340_000.0, 0.5455], [28_010_000.0, 0.8397]]
    sizes = np.array([1e4, 1.2e5, 1e6, 2.33e6, 5e6, 9.34e6, 2.8e7, 1e8])
    s = empirical_slippage(sizes, points)
    assert np.all(np.diff(s) >= 0)
    assert np.isclose(s[1], 0.0001)
    assert np.isclose(s[3], 0.0034)
    assert np.isclose(s[5], 0.5455)
    assert np.isclose(s[7], 0.8397)  # flat beyond the last observed point
    assert s[0] < 0.0001  # linear-in-size below the first point
    # Inversion: the max notional at a slippage cap maps back to that cap.
    cap = 0.0566
    q_star = max_notional_within(points, cap)
    assert 2_330_000.0 < q_star < 9_340_000.0
    assert np.isclose(float(empirical_slippage(q_star, points)), cap, rtol=1e-6)


def test_process_chunk_empirical_depth_stalls_beyond_cliff():
    risk = RiskParams(ltv=0.70, liquidation_threshold=0.80, liquidation_bonus=0.05)
    points = [[1e6, 0.001], [5e6, 0.01], [10e6, 0.60]]
    # Collateral value $6m, debt $12m: fully liquidatable, sale = $6m,
    # which sits inside the interpolated region on quiet depth.
    book = PositionBook(np.array([12_000_000.0]), np.array([6_000.0]), np.ones(1))
    price = np.array([1_000.0])
    quiet = process_chunk(
        book, price, np.array([0.0]), risk, 0.10,
        depth_points=points, depth_haircut=np.array([0.0]),
    )
    stressed = process_chunk(
        book, price, np.array([0.0]), risk, 0.10,
        depth_points=points, depth_haircut=np.array([0.6]),
    )
    # The whole queue is submitted but stalls above break-even: queued
    # volume is the sale size, cleared volume is zero.
    assert np.isclose(quiet.queued_usd[0], 6_000_000.0)
    assert quiet.liquidated_usd[0] == 0.0
    assert 0.01 < quiet.slippage[0] < 0.60
    # Haircut scales effective size: $6m / (1 - 0.6) = $15m is past the last
    # point, where the curve saturates at the worst observed slippage.
    assert np.isclose(stressed.slippage[0], 0.60)
    assert stressed.slippage[0] > quiet.slippage[0]
    assert stressed.bad_debt[0] > quiet.bad_debt[0] > 0


def test_clearance_empirical_branch_uses_observed_cliff():
    snap = _snapshot()
    snap.depth.points = [
        [120_000.0, 0.0001], [2_330_000.0, 0.0034],
        [9_340_000.0, 0.5455], [28_010_000.0, 0.8397],
    ]
    res = arfc_clearance_test(snap, stressed_haircut=0.5)
    # Break-even 5.66% is crossed between $2.33m and $9.34m.
    assert 2_330_000.0 < res.max_clearable_usd_quiet < 9_340_000.0
    assert np.isclose(res.max_clearable_usd_stressed, res.max_clearable_usd_quiet * 0.5)
    # Largest sale ($21.2m of wstETH) is far beyond the cliff: FAIL.
    assert not res.passes_quiet
    assert not res.passes_stressed
    assert not res.slippage_quiet_is_lower_bound
    assert res.slippage_stressed_is_lower_bound


def test_clearance_math_and_verdicts():
    snap = _snapshot()
    res = arfc_clearance_test(snap, stressed_haircut=0.5)
    # Largest sale: 0xaa full liquidation sells debt * (1 + bonus).
    assert np.isclose(res.largest_borrower_usd, 20_000_000.0 * 1.06)
    assert res.largest_account == "0xaa"
    assert np.isclose(res.largest_debt_usd, 20_000_000.0)
    assert np.isclose(res.largest_target_collateral_usd, 38_000_000.0)
    assert np.isclose(res.breakeven_slippage, 0.06 / 1.06)
    # At the break-even boundary the max-clearable formula is exact.
    l0 = calibrate_liquidity(2_000.0, 25_000_000.0, 0.02)
    q_star = max_clearable_notional(l0, 2_000.0, 0.06)
    assert np.isclose(float(slippage(q_star, l0, 2_000.0)), res.breakeven_slippage)
    assert res.max_clearable_usd_stressed < res.max_clearable_usd_quiet
    assert res.passes_quiet  # ~$21m sale, break-even allows ~$73m quiet
    assert res.passes_stressed


def test_clearance_chart_marks_unquoted_whale_without_fake_slippage_point():
    snap = _snapshot()
    snap.depth.points = [
        [120_000.0, 0.0001], [2_330_000.0, 0.0034], [9_340_000.0, 0.5455]
    ]
    res = arfc_clearance_test(snap, stressed_haircut=0.5)
    figure = empirical_depth_fig(snap, res)
    assert figure.layout.xaxis.type == "log"
    assert figure.layout.xaxis.title.text.endswith("log scale)")
    assert figure.layout.xaxis.range[1] < 4
    names = [trace.name for trace in figure.data]
    assert f"{snap.depth.source} quotes" in names
    assert "outside quoted range" in names
    assert "max clearable" in names
    assert "largest borrower sale" in names


def test_real_book_runs_through_engine():
    from aave_risk_engine.config import SimConfig
    from aave_risk_engine.engine import RiskEngine

    snap = _snapshot()
    cfg = scenario_config_from_snapshot(snap)
    cfg.sim = SimConfig(n_scenarios=4_000, seed=1, chunk_size=2_000)
    engine = RiskEngine(cfg)
    book = build_real_book(snap)
    base = engine.run(book=book)
    assert np.isclose(base.total_debt, book.total_debt)
    assert np.all(base.bad_debt >= 0)
    doubled = engine.run(book=book, total_debt_usd=book.total_debt * 2.0)
    assert doubled.cvar >= base.cvar


def test_eth_debt_scales_with_scenario_return():
    risk = RiskParams(ltv=0.70, liquidation_threshold=0.80, liquidation_bonus=0.05)
    depth = np.array([1e12, 1e12])
    # Two identical positions except for debt denomination. Collateral is
    # ETH-correlated: a -40% ETH scenario cuts the collateral price 40%.
    price = np.array([60.0, 100.0])
    eth_ret = np.array([-0.40, 0.0])
    debt = np.array([80.0])
    units = np.array([1.0])
    stable = PositionBook(debt, units, np.ones(1))
    looper = PositionBook(debt, units, np.ones(1), eth_debt_usd=np.array([80.0]))

    res_stable = process_chunk(stable, price, depth, risk, 0.0, eth_return=eth_ret)
    res_looper = process_chunk(looper, price, depth, risk, 0.0, eth_return=eth_ret)
    # Stable debt: HF = 60*0.8/80 = 0.6, liquidated. Looper: debt falls to
    # 48, HF = 60*0.8/48 = 1.0, not liquidated.
    assert res_stable.liquidated_usd[0] > 0
    assert res_looper.liquidated_usd[0] == 0
    # Flat ETH scenario: both books behave identically.
    assert np.isclose(res_stable.liquidated_usd[1], res_looper.liquidated_usd[1])

    # A pure exchange-rate shock (collateral down, ETH flat) hits the looper.
    peg_price = np.array([88.0])
    res_peg = process_chunk(
        looper, peg_price, depth[:1], risk, 0.0, eth_return=np.array([0.0])
    )
    # HF = 88*0.8/80 = 0.88: liquidatable purely from the ratio move.
    assert res_peg.liquidated_usd[0] > 0


def test_scale_book_preserves_eth_debt_fraction():
    book = PositionBook(
        np.array([100.0, 50.0]),
        np.array([1.0, 1.0]),
        np.ones(2),
        eth_debt_usd=np.array([100.0, 0.0]),
    )
    scaled = scale_book(book, 300.0)
    assert np.isclose(scaled.eth_debt_usd[0] / scaled.debt_usd[0], 1.0)
    assert np.isclose(scaled.eth_debt_usd[1], 0.0)


def test_model_eth_debt_includes_loopers_with_split():
    snap = _snapshot()
    book = build_real_book(snap, model_eth_debt=True)
    # The looper 0xff is included and carries its ETH-denominated portion.
    assert book.debt_usd.size == 4
    assert book.eth_debt_usd is not None
    by_debt = dict(zip(book.debt_usd, book.eth_debt_usd))
    assert np.isclose(by_debt[10_000_000.0], 9_000_000.0)
    assert np.isclose(by_debt[20_000_000.0], 0.0)


def test_combined_book_cvar_at_least_usd_only():
    from aave_risk_engine.config import SimConfig
    from aave_risk_engine.engine import RiskEngine

    snap = _snapshot()
    cfg = scenario_config_from_snapshot(snap)
    cfg.sim = SimConfig(n_scenarios=4_000, seed=1, chunk_size=2_000)
    engine = RiskEngine(cfg)
    usd_only = engine.run(book=build_real_book(snap))
    combined = engine.run(book=build_real_book(snap, model_eth_debt=True))
    # Adding positions can only add nonnegative per-position bad debt.
    assert combined.cvar >= usd_only.cvar


def test_unpegged_asset_gets_zero_peg_stress():
    snap = _snapshot()
    # No peg series calibrated: the asset is its own underlying (e.g. WETH).
    snap.stress.peg_pass = None
    snap.stress.peg_daily_vol = None
    cfg = scenario_config_from_snapshot(snap)
    assert cfg.stress.base_peg_drop == 0.0
    assert cfg.stress.peg_crash_beta == 0.0
    assert cfg.stress.peg_idio_vol == 0.0
    # Pegged asset keeps its peg stress terms.
    pegged = scenario_config_from_snapshot(_snapshot())
    assert pegged.stress.peg_crash_beta > 0


def test_peg_vol_calibration_wires_into_config():
    from aave_risk_engine.data.markets import ratio_daily_vol

    rng = np.random.default_rng(2)
    ratios = np.exp(np.cumsum(0.002 * rng.standard_normal(400)))
    vol = ratio_daily_vol(ratios)
    assert 0.0015 < vol < 0.0025

    snap = _snapshot()
    snap.stress.peg_daily_vol = 0.002
    cfg = scenario_config_from_snapshot(snap)
    assert np.isclose(cfg.stress.peg_idio_vol, 0.002 * np.sqrt(cfg.stress.horizon_days))


def test_lst_ratio_cleaning():
    from aave_risk_engine.data.markets import clean_lst_ratio

    # Above-par prints are capped and single-mark spikes are removed;
    # sustained depegs survive.
    raw = np.array([0.99, 1.17, 0.99, 0.99, 0.76, 0.99, 0.96, 0.96, 0.96])
    cleaned = clean_lst_ratio(raw)
    assert cleaned.max() <= 1.0
    assert np.isclose(cleaned[1], 0.99)  # capped then median-smoothed
    assert np.isclose(cleaned[4], 0.99)  # single bad print removed
    assert np.isclose(cleaned[7], 0.96)  # sustained move kept


def test_episode_grid_and_median():
    from aave_risk_engine.data.episodes import _rolling_median3, _to_daily_grid

    dates = ["2022-05-01", "2022-05-02", "2022-05-05"]
    grid_dates, grid = _to_daily_grid(dates, [1.0, 2.0, 8.0])
    assert grid_dates[0] == "2022-05-01" and grid_dates[-1] == "2022-05-05"
    assert grid.size == 5
    assert np.isclose(grid[2], 4.0) and np.isclose(grid[3], 6.0)

    spiked = np.array([1.0, 1.0, 5.0, 1.0, 1.0])
    smoothed = _rolling_median3(spiked)
    assert np.isclose(smoothed[2], 1.0)
    assert np.isclose(smoothed[0], 1.0) and np.isclose(smoothed[-1], 1.0)


def test_episode_windows_cap_ratio_at_par():
    from aave_risk_engine.data.episodes import EpisodePaths, rolling_windows

    # An artifact-high ratio print reverting to normal must not read as a
    # depeg; a genuine drop below par must.
    paths = EpisodePaths(
        name="x",
        dates=[f"2022-05-{d:02d}" for d in range(1, 9)],
        driver_usd=[100.0] * 8,
        ratio=[0.99, 0.99, 1.12, 0.99, 0.99, 0.99, 0.94, 0.94],
    )
    windows = rolling_windows(paths, horizon_days=2, smooth_ratio=False)
    assert np.all(windows["eth_return"] == 0.0)
    # Window starting at the capped 1.12 print: 1.00 -> 0.99 is a 1% drop.
    assert windows["peg_drop"][2] <= 0.011
    # Window 0.99 -> 0.94 is a real depeg of about 5%.
    assert np.isclose(windows["peg_drop"].max(), 1.0 - 0.94 / 0.99)


def test_episode_scenarios_shapes_and_pricing():
    from aave_risk_engine.data.episodes import EpisodePaths, episode_scenarios

    snap = _snapshot()
    cfg = scenario_config_from_snapshot(snap)
    paths = EpisodePaths(
        name="x",
        dates=[f"2022-06-{d:02d}" for d in range(1, 7)],
        driver_usd=[100.0, 90.0, 80.0, 80.0, 80.0, 80.0],
        ratio=[1.0, 1.0, 1.0, 0.95, 0.95, 0.95],
    )
    scen = episode_scenarios(paths, cfg, depth_haircut=0.4)
    assert scen.coll_price.size == 4  # six days, two-day horizon
    spot = cfg.asset.spot_price
    assert np.isclose(scen.coll_price[0], spot * 0.8)  # -20%, peg intact
    assert np.isclose(scen.coll_price[2], spot * 1.0 * 0.95)  # peg-only window
    assert np.all(scen.depth_haircut == 0.4)


def test_episode_replay_hits_looper_book_on_peg_drop():
    from aave_risk_engine.data.episodes import EpisodePaths, episode_scenarios
    from aave_risk_engine.engine import evaluate_book

    snap = _snapshot()
    cfg = scenario_config_from_snapshot(snap)
    # Pure exchange-rate episode: driver flat, ratio drops 12%.
    paths = EpisodePaths(
        name="x",
        dates=[f"2022-06-{d:02d}" for d in range(1, 8)],
        driver_usd=[100.0] * 7,
        ratio=[1.0, 1.0, 1.0, 0.88, 0.88, 0.88, 0.88],
    )
    scen = episode_scenarios(paths, cfg)
    args = (cfg.risk, cfg.stress.liquidation_delay_drawdown, cfg.sim.cvar_level)
    usd = evaluate_book(build_real_book(snap), scen, *args)
    combined = evaluate_book(build_real_book(snap, model_eth_debt=True), scen, *args)
    # The looper (HF 1.6 at LT 0.80 needs a bigger move; use worst window).
    # A 12% ratio drop cuts collateral 12% while ETH-denominated debt is
    # unchanged, so the combined book can only be at least as bad.
    assert combined.worst >= usd.worst


def test_committed_episodes_load_offline():
    from aave_risk_engine.data.episodes import (
        EPISODES,
        episode_path_file,
        load_episode_paths,
        rolling_windows,
    )

    found = 0
    for name in EPISODES:
        if not os.path.exists(episode_path_file(name)):
            continue
        found += 1
        paths = load_episode_paths(name)
        windows = rolling_windows(paths, 2)
        assert windows["eth_return"].size > 0
        assert np.all(windows["peg_drop"] >= 0.0)
        assert np.all(windows["peg_drop"] < 0.5)
    if not found:
        print("  (skipped: no committed episodes)")


def test_chain_configs_are_well_formed():
    from aave_risk_engine.data.aave_v3 import CHAINS, TOKENS

    assert "ethereum" in CHAINS and "linea" in CHAINS
    for chain in CHAINS.values():
        for address in (chain.addresses_provider, chain.pool, *chain.tokens.values()):
            assert address.startswith("0x") and len(address) == 42
        assert chain.rpc_endpoints and chain.log_endpoints
        assert "WETH" in chain.tokens  # needed for the ETH-debt measurement
        assert chain.paraswap_network is not None or chain.kyber_slug is not None
    assert CHAINS["ethereum"].tokens is TOKENS
    assert CHAINS["ethereum"].pool.lower() == "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"


def test_committed_snapshot_loads_offline():
    from aave_risk_engine.data.snapshot import default_snapshot_path

    path = default_snapshot_path()
    if not os.path.exists(path):
        print("  (skipped: no committed snapshot)")
        return
    snap = load_snapshot(path)
    assert snap.reserve.symbol == "wstETH"
    assert 0 < snap.reserve.ltv <= snap.reserve.liquidation_threshold < 1
    assert snap.reserve.price_usd > 0
    assert len(snap.accounts) > 0
    book = build_real_book(snap)
    assert book.total_debt > 0
    res = arfc_clearance_test(snap)
    assert res.breakeven_slippage > 0


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} data tests passed.")


if __name__ == "__main__":
    _run_all()
