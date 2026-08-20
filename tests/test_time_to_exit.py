"""Tests for deterministic time-to-exit liquidation capacity."""

from __future__ import annotations

import os

import numpy as np

from aave_risk_engine.data import arfc_clearance_test, load_snapshot
from aave_risk_engine.data.snapshot import default_snapshot_path
from aave_risk_engine.time_to_exit import (
    ExitAssumptions,
    build_exit_curve,
    conditional_stalled_loss,
    dex_capacity_at,
    redemption_capacity_at,
    required_redemption_usd_per_day,
    time_to_clear_hours,
)


def test_dex_capacity_adds_one_equivalent_batch_per_refill():
    assumptions = ExitAssumptions(instant_dex_capacity_usd=100.0, dex_refill_hours=6.0)
    assert dex_capacity_at(assumptions, 0.0) == 100.0
    assert dex_capacity_at(assumptions, 6.0) == 200.0
    assert dex_capacity_at(assumptions, 24.0) == 500.0


def test_no_refill_keeps_dex_capacity_constant():
    assumptions = ExitAssumptions(instant_dex_capacity_usd=100.0, dex_refill_hours=None)
    assert dex_capacity_at(assumptions, 0.0) == 100.0
    assert dex_capacity_at(assumptions, 168.0) == 100.0


def test_redemption_capacity_starts_only_after_delay():
    assumptions = ExitAssumptions(
        instant_dex_capacity_usd=100.0,
        dex_refill_hours=None,
        redemption_capacity_usd_per_day=240.0,
        redemption_delay_hours=24.0,
    )
    assert redemption_capacity_at(assumptions, 24.0) == 0.0
    assert redemption_capacity_at(assumptions, 30.0) == 60.0
    assert redemption_capacity_at(assumptions, 48.0) == 240.0


def test_time_to_clear_solves_piecewise_refill_and_redemption():
    assumptions = ExitAssumptions(
        instant_dex_capacity_usd=100.0,
        dex_refill_hours=10.0,
        redemption_capacity_usd_per_day=240.0,
        redemption_delay_hours=20.0,
    )
    # Capacity is 300 at the delay, then grows by 20 per hour.
    assert np.isclose(time_to_clear_hours(500.0, assumptions), 30.0)
    assert time_to_clear_hours(80.0, assumptions) == 0.0


def test_required_redemption_throughput_closes_horizon_gap():
    required = required_redemption_usd_per_day(
        sale_usd=500.0,
        instant_dex_capacity_usd=100.0,
        dex_refill_hours=10.0,
        horizon_hours=30.0,
        redemption_delay_hours=10.0,
    )
    # DEX capacity is 400, leaving 100 over 20 active redemption hours.
    assert np.isclose(required, 120.0)
    assert (
        required_redemption_usd_per_day(500.0, 100.0, 10.0, 10.0, 10.0)
        is None
    )


def test_conditional_loss_starts_above_liquidator_break_even():
    bonus = 0.05
    break_even = bonus / (1.0 + bonus)
    debt, loss = conditional_stalled_loss(105.0, bonus, break_even)
    assert np.isclose(debt, 100.0)
    assert np.isclose(loss, 0.0)
    _, stressed_loss = conditional_stalled_loss(105.0, bonus, 0.10)
    assert np.isclose(stressed_loss, 5.5)


def test_exit_curve_is_monotone_and_marks_only_unresolved_sale():
    assumptions = ExitAssumptions(
        instant_dex_capacity_usd=100.0,
        dex_refill_hours=10.0,
        redemption_capacity_usd_per_day=240.0,
        redemption_delay_hours=20.0,
        stalled_drawdown=0.10,
    )
    curve = build_exit_curve(500.0, 0.05, assumptions, (0.0, 10.0, 20.0, 30.0))
    capacities = [point.total_capacity_usd for point in curve.points]
    unresolved = [point.unresolved_sale_usd for point in curve.points]
    assert capacities == sorted(capacities)
    assert unresolved == sorted(unresolved, reverse=True)
    assert curve.points[-1].passes
    assert curve.points[-1].conditional_bad_debt_usd == 0.0


def test_release_wsteth_requires_horizon_capacity():
    path = default_snapshot_path()
    if not os.path.exists(path):
        print("  (skipped: no committed snapshot)")
        return
    snapshot = load_snapshot(path)
    clearance = arfc_clearance_test(snapshot)
    assumptions = ExitAssumptions(
        instant_dex_capacity_usd=clearance.max_clearable_usd_quiet,
        dex_refill_hours=6.0,
    )
    curve = build_exit_curve(
        clearance.largest_borrower_usd,
        snapshot.reserve.liquidation_bonus,
        assumptions,
    )
    assert not clearance.passes_quiet
    assert curve.time_to_clear_hours is not None
    assert curve.time_to_clear_hours > 7.0 * 24.0


def _run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} time-to-exit tests passed.")


if __name__ == "__main__":
    _run_all()
