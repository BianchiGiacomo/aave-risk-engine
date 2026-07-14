"""Sanity tests for the single-Spoke risk engine."""

from __future__ import annotations

import numpy as np
from scipy.stats import kurtosis, skew

from aave_risk_engine.config import (
    AssetParams,
    PositionConfig,
    RiskParams,
    ScenarioConfig,
    SimConfig,
    StressConfig,
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
