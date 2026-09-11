"""Tests for the simultaneous liquidation requirement (test 2)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from aave_risk_engine.config import RiskParams
from aave_risk_engine.positions import PositionBook
from aave_risk_engine.simultaneous_requirement import compute
from aave_risk_engine.stress import Scenarios

_RISK = RiskParams(ltv=0.785, liquidation_threshold=0.81, liquidation_bonus=0.06)
_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "manifests"
    / "ethereum-wsteth-simultaneous-requirement-2026-08-18.json"
)


def _scenarios(prices, eth_returns=None) -> Scenarios:
    prices = np.asarray(prices, dtype=float)
    zeros = np.zeros_like(prices)
    eth = zeros if eth_returns is None else np.asarray(eth_returns, dtype=float)
    return Scenarios(
        coll_price=prices,
        depth_liquidity=zeros,
        eth_return=eth,
        peg_drop=zeros,
        depth_haircut=zeros,
    )


def _book(debt, units, lt, eth_debt=None) -> PositionBook:
    debt = np.asarray(debt, dtype=float)
    units = np.asarray(units, dtype=float)
    lt = np.asarray(lt, dtype=float)
    return PositionBook(
        debt_usd=debt,
        coll_units=units,
        hf0=units * 100.0 * lt / debt,
        lt=lt,
        eth_debt_usd=None if eth_debt is None else np.asarray(eth_debt, dtype=float),
    )


def test_close_factor_and_collateral_cap_follow_protocol_sizing():
    book = _book([100.0], [1.0], [0.8])
    # HF = price * 0.8 / 100: safe, half-closable, fully closable, capped.
    result = compute(book, _scenarios([200.0, 124.0, 110.0, 50.0]), _RISK, 0.75)
    repay = result.repayment_usd
    # The tail is the single worst scenario by repayment: full close at 110.
    assert result.tail_scenarios == 1
    assert abs(repay.maximum - 100.0) < 1e-9
    assert abs(result.seizure_usd.maximum - 106.0) < 1e-9
    assert abs(result.prob_any_liquidation - 0.75) < 1e-12


def test_repayment_and_seizure_are_reported_in_their_own_units():
    book = _book([100.0], [1.0], [0.8])
    result = compute(book, _scenarios([124.0]), _RISK, 0.5)
    # Half close at HF 0.992: 50 repaid, 53 of collateral seized.
    assert abs(result.repayment_usd.maximum - 50.0) < 1e-9
    assert abs(result.seizure_usd.maximum - 53.0) < 1e-9


def test_seizure_is_capped_by_available_collateral():
    book = _book([100.0], [1.0], [0.8])
    result = compute(book, _scenarios([50.0]), _RISK, 0.5)
    assert abs(result.seizure_usd.maximum - 50.0) < 1e-9
    assert abs(result.repayment_usd.maximum - 50.0 / 1.06) < 1e-9


def test_eth_denominated_debt_moves_with_the_eth_return():
    # Same collateral price, two debt denominations. The ETH debt falls with
    # a 20% ETH drop and the position stays healthy; the USD debt does not.
    usd = _book([100.0], [1.0], [0.8])
    eth = _book([100.0], [1.0], [0.8], eth_debt=[100.0])
    scenarios = _scenarios([120.0], eth_returns=[-0.20])
    assert compute(usd, scenarios, _RISK, 0.5).liquidatable_positions.maximum == 1
    assert compute(eth, scenarios, _RISK, 0.5).liquidatable_positions.maximum == 0


def test_simultaneous_positions_are_summed_per_scenario():
    book = _book([100.0, 100.0], [1.0, 1.0], [0.8, 0.8])
    result = compute(book, _scenarios([110.0]), _RISK, 0.5)
    assert result.liquidatable_positions.maximum == 2
    assert abs(result.repayment_usd.maximum - 200.0) < 1e-9
    # Two equal positions: the largest is half the total.
    assert abs(result.largest_share_of_tail_repayment - 0.5) < 1e-12


def test_tail_count_matches_the_engine_convention():
    book = _book([100.0], [1.0], [0.8])
    prices = np.linspace(50.0, 200.0, 20_000)
    result = compute(book, _scenarios(prices), _RISK, 0.99)
    # (1 - 0.99) * 20000 overshoots 200 in floating point; the engine's
    # guard keeps the count at 200.
    assert result.tail_scenarios == 200


def test_invalid_tail_level_is_rejected():
    book = _book([100.0], [1.0], [0.8])
    for level in (0.0, 1.0, 1.5):
        try:
            compute(book, _scenarios([100.0]), _RISK, level)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for tail level {level}")


def test_published_manifest_reproduces_the_market_report():
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    stress = manifest["stress_path"]
    assert stress["reproduces_published_scenarios"]
    assert abs(
        stress["reproduced_combined_cvar99"] - stress["published_combined_cvar99"]
    ) < 1e-6
    assert manifest["snapshot"]["block"] == 25_780_402
    results = manifest["results"]
    assert results["tail_scenarios"] == 200
    # The static single-position bound is far above the tail requirement.
    assert (
        results["static_bound_largest_full_seizure_usd"]
        > 10 * results["seizure_usd"]["tail_mean"]
    )
    assert results["seizure_usd"]["p99"] > results["repayment_usd"]["p99"]


def _run_all():
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_")
    ]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} simultaneous requirement tests passed.")


if __name__ == "__main__":
    _run_all()
