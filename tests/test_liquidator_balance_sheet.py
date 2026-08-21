"""Tests for liquidator warehouse cash-flow accounting."""

from __future__ import annotations

import os
from dataclasses import replace

import numpy as np

from aave_risk_engine.data import arfc_clearance_test, load_snapshot
from aave_risk_engine.data.snapshot import default_snapshot_path
from aave_risk_engine.liquidator_balance_sheet import (
    LiquidatorAssumptions,
    break_even_canonical_loss,
    break_even_dex_market_discount,
    minimum_liquidation_bonus,
    simulate_liquidator_balance_sheet,
)
from aave_risk_engine.time_to_exit import ExitAssumptions


def _zero_costs(**overrides) -> LiquidatorAssumptions:
    values = {
        "funding_annual_rate": 0.0,
        "hurdle_annual_rate": 0.0,
        "hedge_entry_cost": 0.0,
        "hedge_carry_annual_rate": 0.0,
        "dex_execution_loss": 0.0,
        "redemption_loss": 0.0,
        "canonical_loss": 0.0,
        "dex_market_discount": 0.0,
        "step_hours": 0.25,
    }
    values.update(overrides)
    return LiquidatorAssumptions(**values)


def test_instant_zero_cost_exit_realizes_full_bonus():
    exit_assumptions = ExitAssumptions(106.0, None)
    result = simulate_liquidator_balance_sheet(
        100.0, 106.0, exit_assumptions, _zero_costs()
    )
    assert result.cleared
    assert result.time_to_clear_hours == 0.0
    assert np.isclose(result.accounting_profit_usd, 6.0)
    assert np.isclose(result.accounting_roi, 0.06)
    assert result.economic_clearance_pass


def test_instant_slippage_break_even_matches_bonus_formula():
    bonus = 0.05
    sale = 100.0 * (1.0 + bonus)
    break_even = bonus / (1.0 + bonus)
    result = simulate_liquidator_balance_sheet(
        100.0,
        sale,
        ExitAssumptions(sale, None),
        _zero_costs(dex_execution_loss=break_even),
    )
    assert np.isclose(result.accounting_profit_usd, 0.0, atol=1e-12)


def test_delayed_recovery_adds_funding_and_reduces_profit():
    instant = simulate_liquidator_balance_sheet(
        100.0,
        106.0,
        ExitAssumptions(106.0, None),
        _zero_costs(funding_annual_rate=0.20),
    )
    delayed = simulate_liquidator_balance_sheet(
        100.0,
        106.0,
        ExitAssumptions(
            0.0,
            None,
            redemption_capacity_usd_per_day=2400.0,
            redemption_delay_hours=24.0,
        ),
        _zero_costs(funding_annual_rate=0.20),
    )
    assert delayed.cleared
    assert delayed.time_to_clear_hours > 24.0
    assert delayed.funding_cost_usd > instant.funding_cost_usd
    assert delayed.accounting_profit_usd < instant.accounting_profit_usd


def test_exit_routes_do_not_double_count_collateral():
    sale = 200.0
    result = simulate_liquidator_balance_sheet(
        180.0,
        sale,
        ExitAssumptions(
            50.0,
            10.0,
            redemption_capacity_usd_per_day=480.0,
            redemption_delay_hours=0.0,
        ),
        _zero_costs(),
    )
    assert result.cleared
    assert np.isclose(result.dex_exit_usd + result.redemption_exit_usd, sale)
    assert np.isclose(result.unresolved_collateral_usd, 0.0)


def test_route_optimizer_waits_when_redemption_dominates_dex_recovery():
    exit_assumptions = ExitAssumptions(
        106.0,
        None,
        redemption_capacity_usd_per_day=106.0,
        redemption_delay_hours=0.0,
    )
    optimized = simulate_liquidator_balance_sheet(
        100.0,
        106.0,
        exit_assumptions,
        _zero_costs(dex_execution_loss=0.10),
    )
    capacity_first = simulate_liquidator_balance_sheet(
        100.0,
        106.0,
        exit_assumptions,
        _zero_costs(
            dex_execution_loss=0.10,
            route_strategy="capacity_first",
        ),
    )
    assert np.isclose(optimized.dex_exit_usd, 0.0)
    assert np.isclose(optimized.redemption_exit_usd, 106.0)
    assert np.isclose(capacity_first.dex_exit_usd, 106.0)
    assert optimized.economic_profit_usd > capacity_first.economic_profit_usd


def test_route_optimizer_uses_dex_when_redemption_wait_is_too_expensive():
    result = simulate_liquidator_balance_sheet(
        100.0,
        106.0,
        ExitAssumptions(
            106.0,
            None,
            redemption_capacity_usd_per_day=106.0,
            redemption_delay_hours=30.0 * 24.0,
        ),
        _zero_costs(
            funding_annual_rate=1.0,
            hedge_carry_annual_rate=1.0,
            dex_execution_loss=0.01,
        ),
    )
    assert np.isclose(result.dex_exit_usd, 106.0)
    assert np.isclose(result.redemption_exit_usd, 0.0)
    assert result.time_to_clear_hours == 0.0


def test_route_optimizer_can_select_an_interior_split():
    exit_assumptions = ExitAssumptions(
        106.0,
        None,
        redemption_capacity_usd_per_day=106.0,
        redemption_delay_hours=30.0 * 24.0,
    )
    assumptions = _zero_costs(
        funding_annual_rate=1.0,
        dex_execution_loss=0.01,
    )
    optimized = simulate_liquidator_balance_sheet(
        100.0,
        106.0,
        exit_assumptions,
        assumptions,
    )
    capacity_first = simulate_liquidator_balance_sheet(
        100.0,
        106.0,
        exit_assumptions,
        replace(assumptions, route_strategy="capacity_first"),
    )
    assert 0.0 < optimized.dex_exit_usd < 106.0
    assert 0.0 < optimized.redemption_exit_usd < 106.0
    assert optimized.economic_profit_usd > capacity_first.economic_profit_usd


def test_route_strategy_rejects_unknown_policy():
    try:
        LiquidatorAssumptions(route_strategy="unknown")
    except ValueError as exc:
        assert "route strategy" in str(exc)
    else:
        raise AssertionError("unknown route strategy was accepted")


def test_lower_stressed_redemption_reduces_profit_and_slows_exit():
    assumptions = LiquidatorAssumptions(canonical_loss=0.04)
    matched = simulate_liquidator_balance_sheet(
        100.0,
        106.0,
        ExitAssumptions(
            1.0,
            24.0,
            redemption_capacity_usd_per_day=25.0,
            redemption_delay_hours=24.0,
        ),
        assumptions,
    )
    degraded = simulate_liquidator_balance_sheet(
        100.0,
        106.0,
        ExitAssumptions(
            1.0,
            24.0,
            redemption_capacity_usd_per_day=12.5,
            redemption_delay_hours=24.0,
        ),
        assumptions,
    )
    assert degraded.cleared
    assert degraded.time_to_clear_hours > matched.time_to_clear_hours
    assert degraded.economic_profit_usd < matched.economic_profit_usd


def test_canonical_and_hedge_costs_reduce_economic_profit():
    exit_assumptions = ExitAssumptions(106.0, None)
    baseline = simulate_liquidator_balance_sheet(
        100.0, 106.0, exit_assumptions, _zero_costs()
    )
    stressed = simulate_liquidator_balance_sheet(
        100.0,
        106.0,
        exit_assumptions,
        _zero_costs(canonical_loss=0.03, hedge_entry_cost=0.01),
    )
    assert stressed.economic_profit_usd < baseline.economic_profit_usd
    assert np.isclose(stressed.hedge_entry_cost_usd, 1.06)


def test_break_even_canonical_loss_recovers_instant_bonus_buffer():
    bonus = 0.06
    sale = 100.0 * (1.0 + bonus)
    result = break_even_canonical_loss(
        100.0,
        sale,
        ExitAssumptions(sale, None),
        _zero_costs(),
    )
    assert result is not None
    assert np.isclose(result, bonus / (1.0 + bonus), atol=2e-7)


def test_dex_market_discount_does_not_reduce_redemption_recovery():
    exit_assumptions = ExitAssumptions(
        0.0,
        None,
        redemption_capacity_usd_per_day=2544.0,
        redemption_delay_hours=0.0,
    )
    market_discount = simulate_liquidator_balance_sheet(
        100.0,
        106.0,
        exit_assumptions,
        _zero_costs(dex_market_discount=0.50),
    )
    canonical_loss = simulate_liquidator_balance_sheet(
        100.0,
        106.0,
        exit_assumptions,
        _zero_costs(canonical_loss=0.10),
    )
    assert np.isclose(market_discount.realized_recovery_usd, 106.0)
    assert np.isclose(canonical_loss.realized_recovery_usd, 95.4)


def test_dex_recovery_combines_canonical_market_and_execution_losses():
    result = simulate_liquidator_balance_sheet(
        100.0,
        106.0,
        ExitAssumptions(106.0, None),
        _zero_costs(
            canonical_loss=0.10,
            dex_market_discount=0.20,
            dex_execution_loss=0.05,
        ),
    )
    expected = 106.0 * 0.90 * 0.80 * 0.95
    assert np.isclose(result.realized_recovery_usd, expected)


def test_break_even_dex_discount_recovers_instant_bonus_buffer():
    bonus = 0.06
    sale = 100.0 * (1.0 + bonus)
    result = break_even_dex_market_discount(
        100.0,
        sale,
        ExitAssumptions(sale, None),
        _zero_costs(),
    )
    assert result is not None
    assert np.isclose(result, bonus / (1.0 + bonus), atol=2e-7)


def test_minimum_bonus_recovers_execution_loss_threshold():
    execution_loss = 0.05
    result = minimum_liquidation_bonus(
        100.0,
        ExitAssumptions(1000.0, None),
        _zero_costs(dex_execution_loss=execution_loss),
    )
    assert result is not None
    assert np.isclose(result, 1.0 / (1.0 - execution_loss) - 1.0, atol=2e-7)


def test_minimum_bonus_finds_interior_solution_before_horizon_limit():
    result = minimum_liquidation_bonus(
        100.0,
        ExitAssumptions(150.0, None),
        _zero_costs(fixed_cost_usd=5.0),
        max_bonus=1.0,
    )
    assert result is not None
    assert np.isclose(result, 0.05, atol=2e-7)


def test_cash_flow_integration_converges_at_subhour_step():
    exit_assumptions = ExitAssumptions(
        10.0,
        6.0,
        redemption_capacity_usd_per_day=25.0,
        redemption_delay_hours=24.0,
    )
    hourly = simulate_liquidator_balance_sheet(
        100.0, 106.0, exit_assumptions, LiquidatorAssumptions(step_hours=1.0)
    )
    quarter_hour = simulate_liquidator_balance_sheet(
        100.0, 106.0, exit_assumptions, LiquidatorAssumptions(step_hours=0.25)
    )
    assert np.isclose(hourly.time_to_clear_hours, quarter_hour.time_to_clear_hours)
    assert np.isclose(
        hourly.economic_profit_usd,
        quarter_hour.economic_profit_usd,
        rtol=2e-4,
    )


def test_release_wsteth_warehouse_clears_but_requires_large_peak_capital():
    path = default_snapshot_path()
    if not os.path.exists(path):
        print("  (skipped: no committed snapshot)")
        return
    snapshot = load_snapshot(path)
    clearance = arfc_clearance_test(snapshot)
    sale = clearance.largest_borrower_usd
    debt = sale / (1.0 + snapshot.reserve.liquidation_bonus)
    result = simulate_liquidator_balance_sheet(
        debt,
        sale,
        ExitAssumptions(
            clearance.max_clearable_usd_quiet,
            6.0,
            redemption_capacity_usd_per_day=25_000_000.0,
            redemption_delay_hours=24.0,
        ),
        LiquidatorAssumptions(),
    )
    assert not clearance.passes_quiet
    assert result.cleared
    assert result.time_to_clear_hours < 30.0 * 24.0
    assert result.peak_capital_usd >= debt


def test_release_route_optimizer_improves_capacity_first_profit():
    path = default_snapshot_path()
    if not os.path.exists(path):
        print("  (skipped: no committed snapshot)")
        return
    snapshot = load_snapshot(path)
    clearance = arfc_clearance_test(snapshot)
    sale = clearance.largest_borrower_usd
    debt = sale / (1.0 + snapshot.reserve.liquidation_bonus)
    exit_assumptions = ExitAssumptions(
        clearance.max_clearable_usd_quiet,
        6.0,
        redemption_capacity_usd_per_day=25_000_000.0,
        redemption_delay_hours=24.0,
    )
    assumptions = LiquidatorAssumptions(canonical_loss=0.04)
    optimized = simulate_liquidator_balance_sheet(
        debt,
        sale,
        exit_assumptions,
        assumptions,
    )
    capacity_first = simulate_liquidator_balance_sheet(
        debt,
        sale,
        exit_assumptions,
        replace(assumptions, route_strategy="capacity_first"),
    )
    assert optimized.route_optimization_evaluations > 1
    assert optimized.redemption_exit_usd > capacity_first.redemption_exit_usd
    assert optimized.economic_profit_usd > capacity_first.economic_profit_usd


def _run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} liquidator balance-sheet tests passed.")


if __name__ == "__main__":
    _run_all()
