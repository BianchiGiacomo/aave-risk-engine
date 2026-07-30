"""Sanity tests for the single-Spoke risk engine."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
from scipy.stats import kurtosis, skew

from aave_risk_engine.config import (
    AssetParams,
    PositionConfig,
    RiskParams,
    ScenarioConfig,
    SimConfig,
    StressConfig,
    V4Liquidation,
)
from aave_risk_engine.engine import RiskEngine, RiskResult, _largest_cap_within_budget
from aave_risk_engine.liquidation import process_chunk
from aave_risk_engine.positions import PositionBook, build_position_book
from aave_risk_engine.slippage import calibrate_liquidity, executable_value, slippage
from aave_risk_engine.stress import sample_log_returns


def _fast_engine(**overrides):
    cfg = ScenarioConfig(sim=SimConfig(n_scenarios=8_000, seed=1, chunk_size=2_000))
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return RiskEngine(cfg)


def test_slippage_calibration_roundtrip():
    p, n, s_star = 3000.0, 25e6, 0.02
    l = calibrate_liquidity(p, n, s_star)
    assert np.isclose(slippage(n, l, p), s_star)


def test_slippage_monotone_and_dollar_impact_convex():
    p = 3000.0
    l = calibrate_liquidity(p, 25e6, 0.02)
    q = np.linspace(1e6, 200e6, 50)
    s = slippage(q, l, p)
    assert np.all(np.diff(s) > 0)
    assert np.all(np.diff(s, 2) < 0)
    assert np.all(np.diff(q * s, 2) > 0)


def test_executable_value_consistent():
    p = 3000.0
    l = calibrate_liquidity(p, 25e6, 0.02)
    q = 10e6
    assert np.isclose(executable_value(q, l, p), q * (1 - slippage(q, l, p)))


def test_bad_debt_nonnegative_and_tail_ordering():
    r = _fast_engine().run()
    assert np.all(r.bad_debt >= 0)
    assert 0 <= r.prob_bad_debt <= 1
    assert r.mean <= r.var <= r.cvar <= r.worst


def test_cvar_uses_worst_tail_not_all_var_ties():
    losses = np.r_[np.zeros(999), 100.0]
    r = RiskResult(
        losses,
        np.zeros_like(losses),
        np.zeros_like(losses),
        np.zeros_like(losses),
        total_debt=1.0,
        cvar_level=0.99,
    )
    assert r.var == 0.0
    assert np.isclose(r.cvar, 10.0)


def test_origination_ltv_sets_minimum_health_factor():
    pos = PositionConfig(n_borrowers=16, hf0_median=1.05, hf0_sigma=0.0, hf0_floor=1.0)
    asset = AssetParams(spot_price=1.0)
    loose = RiskParams(ltv=0.84, liquidation_threshold=0.85)
    tight = RiskParams(ltv=0.70, liquidation_threshold=0.85)
    loose_book = build_position_book(pos, loose, asset, np.random.default_rng(1))
    tight_book = build_position_book(pos, tight, asset, np.random.default_rng(1))
    assert loose_book.hf0.min() >= loose.liquidation_threshold / loose.ltv
    assert np.isclose(tight_book.hf0.min(), tight.liquidation_threshold / tight.ltv)
    assert tight_book.hf0.min() > loose_book.hf0.min()


def test_close_factor_sizes_liquidation_queue():
    book = PositionBook(np.array([100.0]), np.array([115.2941176471]), np.array([1.0]))
    price = np.array([1.0])
    depth = np.array([1e12])
    partial = RiskParams(
        ltv=0.80,
        liquidation_threshold=0.85,
        liquidation_bonus=0.05,
        close_factor=0.50,
        full_liquidation_hf=0.95,
    )
    res_partial = process_chunk(book, price, depth, partial, delay_drawdown=0.0)
    assert np.isclose(res_partial.liquidated_usd[0], 52.5)

    full = RiskParams(
        ltv=0.80,
        liquidation_threshold=0.85,
        liquidation_bonus=0.05,
        close_factor=0.50,
        full_liquidation_hf=0.99,
    )
    res_full = process_chunk(book, price, depth, full, delay_drawdown=0.0)
    assert np.isclose(res_full.liquidated_usd[0], 105.0)


def _v4_risk(**overrides) -> RiskParams:
    fields = dict(
        target_health_factor=1.24,
        max_bonus=0.0666,
        liquidation_bonus_factor=0.90,
        hf_max_bonus=0.90,
        close_factor_floor=0.0,
        dust_threshold_usd=0.0,
    )
    fields.update(overrides)
    return RiskParams(ltv=0.70, liquidation_threshold=0.80, v4=V4Liquidation(**fields))


def _single_position(debt: float, coll_value: float, risk: RiskParams):
    book = PositionBook(np.array([debt]), np.array([coll_value]), np.ones(1))
    return process_chunk(book, np.array([1.0]), np.array([1e15]), risk, delay_drawdown=0.0)


def test_v4_repay_restores_target_hf():
    risk = _v4_risk()
    debt, coll = 100.0, 110.0  # HF = 0.88, deep enough for the full bonus
    res = _single_position(debt, coll, risk)
    bonus = 0.0666
    repay = res.liquidated_usd[0] / (1.0 + bonus)
    hf_after = (coll - res.liquidated_usd[0]) * 0.80 / (debt - repay)
    assert np.isclose(hf_after, 1.24)
    # Solvent position at this bonus: no bad debt when the queue clears.
    assert res.bad_debt[0] == 0.0


def test_v4_dynamic_bonus_scales_with_hf():
    # Full liquidation isolates the bonus: seize = debt * (1 + bonus).
    risk = _v4_risk(close_factor_floor=1.0)
    barely = _single_position(100.0, 124.0, risk)  # HF 0.992
    frac = (1.0 - 0.992) / (1.0 - 0.90)
    bonus = 0.0666 * (0.90 + 0.10 * frac)
    assert np.isclose(barely.liquidated_usd[0], 100.0 * (1.0 + bonus), rtol=1e-6)
    deep = _single_position(100.0, 110.0, risk)  # HF 0.88, capped at max
    assert np.isclose(deep.liquidated_usd[0], 100.0 * 1.0666, rtol=1e-6)


def test_v4_close_factor_floor_binds_near_par():
    # Correlated-Spoke config: near par, repay-to-target 1.0137 needs only
    # ~9% of debt, so the 35% launch floor binds.
    correlated = dict(
        target_health_factor=1.0137, liquidation_bonus_factor=1.0, hf_max_bonus=0.99
    )
    debt, coll = 100.0, 124.9  # HF 0.9992
    unfloored = _single_position(debt, coll, _v4_risk(**correlated, close_factor_floor=0.0))
    floored = _single_position(debt, coll, _v4_risk(**correlated, close_factor_floor=0.35))
    assert unfloored.liquidated_usd[0] < 0.35 * debt
    assert floored.liquidated_usd[0] >= 0.35 * debt
    assert floored.liquidated_usd[0] > unfloored.liquidated_usd[0]


def test_v4_unreachable_target_liquidates_fully():
    # target < (1 + bonus) * LT: no partial repayment can reach the target,
    # so the whole position is closed and all collateral is seized.
    risk = RiskParams(
        ltv=0.90,
        liquidation_threshold=0.99,
        v4=V4Liquidation(
            target_health_factor=1.02,
            max_bonus=0.0666,
            liquidation_bonus_factor=1.0,
            hf_max_bonus=0.99,
            close_factor_floor=0.0,
            dust_threshold_usd=0.0,
        ),
    )
    res = _single_position(100.0, 90.9, risk)  # HF 0.9
    assert np.isclose(res.liquidated_usd[0], 90.9, rtol=1e-6)


def test_v4_dust_forces_full_clearance():
    # Repay-to-target would leave a sub-dust remnant, so the position is
    # closed in full instead.
    small = _single_position(100.0, 110.0, _v4_risk())
    dusted = _single_position(100.0, 110.0, _v4_risk(dust_threshold_usd=1_000.0))
    partial_repay = small.liquidated_usd[0] / 1.0666
    assert partial_repay < 100.0  # target sizing alone is partial
    # Full clearance: repay the whole debt, seize debt * (1 + bonus).
    assert np.isclose(dusted.liquidated_usd[0], 100.0 * 1.0666, rtol=1e-6)
    assert dusted.liquidated_usd[0] > small.liquidated_usd[0]


def test_ordered_queue_front_clears_where_aggregate_stalls_all():
    # Two identical positions under V3. Depth is sized so one sale alone
    # stays under break-even but the doubled queue average does not: the
    # aggregate model stalls everything, the ordered model clears the
    # front of the queue.
    from aave_risk_engine.slippage import calibrate_liquidity

    risk = RiskParams(ltv=0.70, liquidation_threshold=0.80, liquidation_bonus=0.05)
    debt = np.array([100.0, 100.0])
    units = np.array([110.0, 110.0])
    book = PositionBook(debt, units, np.ones(2))
    price = np.array([1.0])
    # Break-even 4.76%: 4% at one seize (105), about 7.7% at both.
    liquidity = calibrate_liquidity(1.0, 105.0, 0.04)
    depth = np.array([liquidity])

    aggregate = process_chunk(book, price, depth, risk, delay_drawdown=0.10)
    ordered = process_chunk(
        book, price, depth, replace(risk, ordered_queue=True), delay_drawdown=0.10
    )
    # Aggregate: both stall (queue-average slippage above break-even), so
    # the queue is submitted but nothing clears.
    assert aggregate.slippage[0] > 0.05 / 1.05
    assert aggregate.bad_debt[0] > 0
    assert np.isclose(aggregate.queued_usd[0], 210.0)
    assert aggregate.liquidated_usd[0] == 0.0
    # Ordered: the first tranche clears within its own marginal slippage
    # and is solvent, so only the second (stalled) position contributes.
    assert np.isclose(ordered.queued_usd[0], 210.0)
    assert np.isclose(ordered.liquidated_usd[0], 105.0)
    assert 0 < ordered.bad_debt[0] < aggregate.bad_debt[0]


def test_ordered_queue_matches_aggregate_with_infinite_depth():
    risk = RiskParams(ltv=0.70, liquidation_threshold=0.80, liquidation_bonus=0.05)
    book = PositionBook(np.array([100.0, 80.0]), np.array([110.0, 90.0]), np.ones(2))
    price = np.array([1.0])
    depth = np.array([1e15])
    aggregate = process_chunk(book, price, depth, risk, delay_drawdown=0.10)
    ordered = process_chunk(
        book, price, depth, replace(risk, ordered_queue=True), delay_drawdown=0.10
    )
    assert np.isclose(ordered.bad_debt[0], aggregate.bad_debt[0])
    assert np.isclose(ordered.liquidated_usd[0], aggregate.liquidated_usd[0])


def test_ordered_queue_prioritizes_high_bonus_under_v4():
    # A deep position (full bonus) and a near-par position (smaller bonus)
    # compete for depth that can absorb only the first sale. The deep
    # position must clear first; the near-par one stalls behind it.
    from aave_risk_engine.slippage import calibrate_liquidity

    risk = _v4_risk(close_factor_floor=1.0)
    risk = replace(risk, ordered_queue=True)
    debt = np.array([100.0, 100.0])
    units = np.array([124.0, 110.0])  # HF 0.992 (bonus ~6.05%) and 0.88 (6.66%)
    book = PositionBook(debt, units, np.ones(2))
    price = np.array([1.0])
    # First sale (~106.7) at 5% slippage, below the deep break-even 6.24%;
    # the queue-average of both would be far above every break-even.
    liquidity = calibrate_liquidity(1.0, 106.7, 0.05)
    res = process_chunk(book, price, np.array([liquidity]), risk, delay_drawdown=0.10)
    # Exactly the deep position's seize clears.
    assert np.isclose(res.liquidated_usd[0], 100.0 * 1.0666, rtol=1e-4)
    # The near-par position stalls and is marked at its own marginal
    # slippage, producing bad debt despite being barely under water.
    assert res.bad_debt[0] > 0


def test_v4_config_validation():
    for bad in (
        dict(target_health_factor=1.0),
        dict(liquidation_bonus_factor=0.0),
        dict(hf_max_bonus=1.2),
        dict(close_factor_floor=1.5),
        dict(dust_threshold_usd=-1.0),
    ):
        try:
            V4Liquidation(**bad)
            assert False, f"expected ValueError for {bad}"
        except ValueError:
            pass


def test_cvar_superlinear_in_cap():
    eng = _fast_engine()
    cvar = eng.cap_sweep(np.array([5e7, 1e8, 2e8]))["cvar"]
    assert cvar[2] > 2 * cvar[1] > 0


def test_lower_ltv_reduces_tail_risk():
    sweep = _fast_engine().ltv_sweep(np.array([0.75, 0.90]))
    assert sweep["cvar"][0] < sweep["cvar"][1]


def test_recommend_cap_respects_budget():
    eng = _fast_engine()
    rec = eng.recommend_cap(5e6, cap_min=2e7, cap_max=4e8, n_grid=30)
    r = eng.run(total_debt_usd=rec["recommended_cap"])
    assert np.isfinite(rec["recommended_cap"])
    assert r.cvar <= 5e6 * 1.10


def test_zero_vol_zero_bad_debt():
    cfg = ScenarioConfig(sim=SimConfig(n_scenarios=4_000, seed=3, chunk_size=2_000))
    cfg.stress.eth_annual_vol = 1e-9
    cfg.stress.base_peg_drop = 0.0
    cfg.stress.peg_crash_beta = 0.0
    cfg.stress.peg_idio_vol = 0.0
    r = RiskEngine(cfg).run()
    assert r.mean == 0.0 and r.worst == 0.0


def test_largest_cap_helper():
    caps = np.array([10.0, 20.0, 30.0, 40.0])
    cvar = np.array([1.0, 4.0, 9.0, 16.0])
    cap = _largest_cap_within_budget(caps, cvar, 6.5)
    assert 20.0 < cap < 30.0


def test_return_models_dispatch_and_shape():
    h, n = 2.0 / 365.0, 200_000
    rng = lambda: np.random.default_rng(0)
    gauss = sample_log_returns(StressConfig(return_model="gaussian"), h, n, rng())
    t = sample_log_returns(StressConfig(return_model="student_t", tail_dof=3.0), h, n, rng())
    jump = sample_log_returns(
        StressConfig(return_model="jump_diffusion", jump_mean=-0.15), h, n, rng()
    )

    assert abs(kurtosis(gauss)) < 0.1 and abs(skew(gauss)) < 0.1
    assert kurtosis(t) > kurtosis(gauss)
    assert skew(jump) < -0.1
    assert np.percentile(jump, 1) < np.percentile(gauss, 1)
    again = sample_log_returns(
        StressConfig(return_model="jump_diffusion", jump_mean=-0.15), h, n, rng()
    )
    assert np.array_equal(jump, again)
    try:
        sample_log_returns(StressConfig(return_model="nope"), h, n, rng())
        assert False, "expected ValueError"
    except ValueError:
        pass


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
