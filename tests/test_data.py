"""Offline tests for the market-data layer (no network access needed)."""

from __future__ import annotations

import os
import tempfile

import numpy as np

from aave_risk_engine.config import RiskParams
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
)
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
    assert np.isclose(quiet.liquidated_usd[0], 6_000_000.0)
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


def test_clearance_math_and_verdicts():
    snap = _snapshot()
    res = arfc_clearance_test(snap, stressed_haircut=0.5)
    # Largest sale: 0xaa full liquidation sells debt * (1 + bonus).
    assert np.isclose(res.largest_borrower_usd, 20_000_000.0 * 1.06)
    assert np.isclose(res.breakeven_slippage, 0.06 / 1.06)
    # At the break-even boundary the max-clearable formula is exact.
    l0 = calibrate_liquidity(2_000.0, 25_000_000.0, 0.02)
    q_star = max_clearable_notional(l0, 2_000.0, 0.06)
    assert np.isclose(float(slippage(q_star, l0, 2_000.0)), res.breakeven_slippage)
    assert res.max_clearable_usd_stressed < res.max_clearable_usd_quiet
    assert res.passes_quiet  # ~$21m sale, break-even allows ~$73m quiet
    assert res.passes_stressed


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
