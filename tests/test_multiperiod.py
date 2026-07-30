"""Tests for the multi-period liquidation simulator."""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from aave_risk_engine.config import (
    AssetParams,
    LiquidityParams,
    RiskParams,
    ScenarioConfig,
    SimConfig,
    StressConfig,
    V4Liquidation,
)
from aave_risk_engine.engine import evaluate_book
from aave_risk_engine.multiperiod import simulate_multi_period
from aave_risk_engine.positions import PositionBook
from aave_risk_engine.stress import sample_scenarios


def _quiet_stress(**overrides) -> StressConfig:
    fields = dict(
        horizon_days=2.0,
        eth_annual_vol=0.75,
        base_peg_drop=0.0,
        peg_crash_beta=0.0,
        peg_idio_vol=0.0,
        base_depth_haircut=0.0,
        depth_crash_beta=0.0,
        depth_idio_vol=0.0,
        liquidation_delay_drawdown=0.10,
    )
    fields.update(overrides)
    return StressConfig(**fields)


def _config(risk: RiskParams, stress: StressConfig, depth_usd: float = 1e12) -> ScenarioConfig:
    return ScenarioConfig(
        asset=AssetParams(name="X", spot_price=100.0),
        risk=risk,
        liquidity=LiquidityParams(ref_notional_usd=depth_usd, ref_slippage=0.02),
        stress=stress,
        sim=SimConfig(n_scenarios=4, seed=1, chunk_size=4),
    )


def _book(debts, coll_values, spot=100.0) -> PositionBook:
    debts = np.asarray(debts, dtype=float)
    coll = np.asarray(coll_values, dtype=float)
    return PositionBook(debts, coll / spot, np.ones(debts.size))


def _steps(values, n_paths=1) -> np.ndarray:
    return np.tile(np.asarray(values, dtype=float)[:, None], (1, n_paths))


def test_flat_paths_produce_no_bad_debt():
    risk = RiskParams(ltv=0.70, liquidation_threshold=0.80)
    cfg = _config(risk, _quiet_stress())
    book = _book([100.0], [150.0])
    res = simulate_multi_period(
        cfg, book, n_periods=4, total_days=4.0, n_paths=1,
        step_log_returns=_steps([0.0, 0.0, 0.0, 0.0]),
        peg_idio_steps=_steps([0.0] * 4),
        haircut_idio_steps=_steps([0.0] * 4),
    )
    assert res.bad_debt[0] == 0.0
    assert res.liquidation_events[0] == 0


def test_single_period_matches_engine_ordered_mode():
    # With one period and identical shock inputs, the simulator must agree
    # with the single-period engine in ordered mode: same clears, same
    # stall marks, same insolvency gaps.
    risk = RiskParams(
        ltv=0.70, liquidation_threshold=0.80, liquidation_bonus=0.05, ordered_queue=True
    )
    stress = _quiet_stress(
        base_peg_drop=0.002, peg_crash_beta=0.3, base_depth_haircut=0.1, depth_crash_beta=1.0
    )
    cfg = _config(risk, stress, depth_usd=260.0)
    book = _book([100.0, 90.0, 50.0], [120.0, 112.0, 70.0])

    log_ret = np.array([-0.28, -0.10, 0.0, -0.45])
    scen = sample_scenarios(
        stress, cfg.asset, cfg.liquidity, log_ret.size,
        np.random.default_rng(0), log_return=log_ret,
    )
    single = evaluate_book(book, scen, risk, stress.liquidation_delay_drawdown, 0.99)

    multi = simulate_multi_period(
        cfg, book, n_periods=1, total_days=stress.horizon_days,
        n_paths=log_ret.size,
        step_log_returns=log_ret[None, :],
        peg_idio_steps=np.zeros((1, log_ret.size)),
        haircut_idio_steps=np.zeros((1, log_ret.size)),
    )
    assert np.allclose(multi.bad_debt, single.bad_debt)


def test_reliquidation_frequency_depends_on_target_hf():
    # Two successive drops. A position liquidated to target 1.24 survives
    # the second drop; restored only to 1.0137 it goes under again.
    def run(target):
        risk = RiskParams(
            ltv=0.70,
            liquidation_threshold=0.80,
            v4=V4Liquidation(
                target_health_factor=target,
                max_bonus=0.05,
                liquidation_bonus_factor=1.0,
                hf_max_bonus=0.95,
                close_factor_floor=0.0,
                dust_threshold_usd=0.0,
            ),
        )
        cfg = _config(risk, _quiet_stress())
        book = _book([100.0], [131.0])  # HF 1.048
        steps = np.log(np.array([0.92, 0.92]))
        return simulate_multi_period(
            cfg, book, n_periods=2, total_days=2.0, n_paths=1,
            step_log_returns=_steps(steps),
            peg_idio_steps=_steps([0.0, 0.0]),
            haircut_idio_steps=_steps([0.0, 0.0]),
        )

    high = run(1.24)
    low = run(1.0137)
    assert high.liquidation_events[0] == 1
    assert high.reliquidated_positions[0] == 0
    assert low.liquidation_events[0] == 2
    assert low.reliquidated_positions[0] == 1
    assert high.bad_debt[0] == 0.0 and low.bad_debt[0] == 0.0


def test_v_shape_recovery_rescues_stalled_position():
    # Depth is far too thin to clear in the crash period, so the position
    # stalls. The price then recovers above water before the window ends:
    # no bad debt, where an immediate fire-sale mark would have booked a
    # large loss.
    risk = RiskParams(ltv=0.70, liquidation_threshold=0.80, liquidation_bonus=0.05)
    cfg = _config(risk, _quiet_stress(), depth_usd=5.0)
    book = _book([100.0], [130.0])
    steps = np.log(np.array([0.70, 1.0 / 0.70]))
    res = simulate_multi_period(
        cfg, book, n_periods=2, total_days=2.0, n_paths=1,
        step_log_returns=_steps(steps),
        peg_idio_steps=_steps([0.0, 0.0]),
        haircut_idio_steps=_steps([0.0, 0.0]),
    )
    assert res.liquidation_events[0] == 0
    assert res.bad_debt[0] == 0.0

    # The same crash without the recovery is marked at the window end.
    flat = simulate_multi_period(
        cfg, book, n_periods=2, total_days=2.0, n_paths=1,
        step_log_returns=_steps(np.log(np.array([0.70, 1.0]))),
        peg_idio_steps=_steps([0.0, 0.0]),
        haircut_idio_steps=_steps([0.0, 0.0]),
    )
    assert flat.terminal_marks[0] > 0
    assert flat.bad_debt[0] > 0


def test_no_replenishment_is_at_least_as_bad():
    # A grinding decline with several liquidation rounds through moderate
    # depth: with no depth replenishment, later rounds start deeper in the
    # curve, so losses can only be equal or worse.
    risk = RiskParams(ltv=0.70, liquidation_threshold=0.80, liquidation_bonus=0.05)
    cfg = _config(risk, _quiet_stress(), depth_usd=180.0)
    book = _book([100.0, 80.0, 60.0], [125.0, 101.0, 76.0])
    steps = _steps(np.log(np.array([0.93, 0.93, 0.93, 0.93])))
    kwargs = dict(
        n_periods=4, total_days=4.0, n_paths=1,
        step_log_returns=steps,
        peg_idio_steps=_steps([0.0] * 4),
        haircut_idio_steps=_steps([0.0] * 4),
    )
    full = simulate_multi_period(cfg, book, replenish=1.0, **kwargs)
    none = simulate_multi_period(cfg, book, replenish=0.0, **kwargs)
    assert none.bad_debt[0] >= full.bad_debt[0]
    assert none.bad_debt[0] > 0


def test_idio_step_sigma_scales_with_window():
    from aave_risk_engine.multiperiod import _idio_step_sigma

    # Terminal variance over K steps must equal idio_vol^2 scaled by
    # total_days / horizon_days.
    for total_days, n_periods in ((2.0, 1), (2.0, 4), (4.0, 8), (8.0, 4)):
        sigma = _idio_step_sigma(0.004, 2.0, total_days, n_periods)
        terminal_var = n_periods * sigma**2
        assert np.isclose(terminal_var, 0.004**2 * total_days / 2.0)


def test_summary_properties_consistent():
    risk = RiskParams(ltv=0.70, liquidation_threshold=0.80)
    cfg = _config(risk, replace(_quiet_stress(), eth_annual_vol=1.0))
    cfg.sim = SimConfig(n_scenarios=2_000, seed=3, chunk_size=500)
    book = _book([100.0, 50.0], [140.0, 68.0])
    res = simulate_multi_period(cfg, book, n_periods=4, total_days=4.0)
    assert res.bad_debt.size == 2_000
    assert np.all(res.bad_debt >= 0)
    # mean <= VaR need not hold when P(bad debt) < 1%; the tail chain does.
    assert res.var <= res.cvar <= float(res.bad_debt.max()) + 1e-9
    assert res.mean <= res.cvar
    assert np.allclose(res.bad_debt, res.realized_bad_debt + res.terminal_marks)


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} multiperiod tests passed.")


if __name__ == "__main__":
    _run_all()
