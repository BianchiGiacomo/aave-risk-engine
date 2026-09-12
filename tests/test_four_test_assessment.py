"""Tests for the four-test assessment rules and the worked wstETH case."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from aave_risk_engine.four_test_assessment import (
    FAIL,
    INDETERMINATE,
    PASS,
    FinancingEvidence,
    SubOutcome,
    TestOutcome,
    build_assessment,
    combine,
    flipping_inputs,
    load_manifest,
    reprice_balance_sheet,
    assess_bonus_adequacy,
)
from aave_risk_engine.liquidator_balance_sheet import (
    LiquidatorAssumptions, simulate_liquidator_balance_sheet,
)
from aave_risk_engine.time_to_exit import ExitAssumptions

_MANIFESTS = Path(__file__).resolve().parents[1] / "docs" / "manifests"
_PATHS = {
    "reachability": _MANIFESTS
    / "ethereum-wsteth-oracle-reachability-25780402.json",
    "market": _MANIFESTS / "ethereum-wsteth-2026-08-18.json",
    "simultaneity": _MANIFESTS
    / "ethereum-wsteth-simultaneous-requirement-2026-08-18.json",
    "balance_sheet": _MANIFESTS
    / "ethereum-wsteth-liquidator-balance-sheet-2026-08-18.json",
}
_STRESS = "pinned market report stress path"


def _inputs() -> dict:
    return {
        key: (key, copy.deepcopy(load_manifest(p)))
        for key, p in _PATHS.items()
    }


def _assessment(mutate=None, financing=None):
    inputs = _inputs()
    if mutate:
        mutate({key: value[1] for key, value in inputs.items()})
    return build_assessment(
        inputs["reachability"],
        inputs["market"],
        inputs["simultaneity"],
        inputs["balance_sheet"],
        stress_path=_STRESS,
        financing=financing,
    )


def _outcome(number: int, verdict: str, subs=()) -> TestOutcome:
    return TestOutcome(
        number=number,
        question="q",
        criterion="c",
        verdict=verdict,
        finding="f",
        sub_outcomes=tuple(subs),
    )


def _sweep(sheet: dict) -> dict:
    return sheet["canonical_loss_sweep"]["0.040000"]


# The worked case ----------------------------------------------------------


def test_all_four_manifests_share_one_block():
    result = _assessment()
    assert result.vintage_consistent
    assert result.block == 25_780_402


def test_worked_case_verdicts():
    result = _assessment()
    assert [t.verdict for t in result.tests] == [
        PASS,
        PASS,
        INDETERMINATE,
        PASS,
    ]
    assert result.overall == INDETERMINATE
    assert "3a instant clearance fails within its own scope" in (
        result.overall_reason
    )


def test_every_verdict_cites_at_least_one_manifest_field():
    for test in _assessment().tests:
        assert test.evidence, f"test {test.number} cites no evidence"


# Test 2 -------------------------------------------------------------------


def test_requirement_comes_from_the_stress_path_not_the_static_bound():
    test2 = _assessment().tests[1]
    assert "$7.15m of debt repayment" in test2.finding
    assert "$7.58m of collateral" in test2.finding
    assert "p99" in test2.finding


def test_requirement_distinguishes_debt_from_collateral():
    finding = _assessment().tests[1].finding
    assert "of debt repayment" in finding
    assert "of collateral" in finding
    # No aggregate book debt is compared with a collateral figure.
    assert "combined book" not in finding


def test_requirement_accounts_for_capital_recycling():
    finding = _assessment().tests[1].finding
    assert "no intervening capital recycling is assumed" in finding
    assert "not inferred" in finding


def test_provenance_alone_no_longer_passes_test_two():
    def unreproduced(m):
        path = m["simultaneity"]["stress_path"]
        path["reproduces_published_scenarios"] = False

    assert _assessment(unreproduced).tests[1].verdict == INDETERMINATE

    def incomplete(m):
        discovery = m["market"]["snapshot"]["borrower_discovery"]
        discovery["source"] = "sampled accounts"

    assert _assessment(incomplete).tests[1].verdict == INDETERMINATE


# Test 3 -------------------------------------------------------------------


def test_instant_clearance_fails_without_deciding_financing():
    test3 = _assessment().tests[2]
    instant, warehouse, mixed = test3.sub_outcomes
    assert instant.verdict == FAIL
    assert warehouse.verdict == INDETERMINATE
    assert test3.verdict == INDETERMINATE
    assert mixed.verdict == INDETERMINATE


def test_evidenced_financing_resolves_without_inventing_an_upper_bound():
    enough = FinancingEvidence(50e6, 30.0, "documented credit line")
    assert _assessment(financing=enough).tests[2].verdict == PASS
    short = FinancingEvidence(1e6, 30.0, "one identified credit line")
    assert _assessment(financing=short).tests[2].verdict == INDETERMINATE
    upper = FinancingEvidence(
        1e6, 30.0, "all scoped capacity", exhaustive=True
    )
    assert _assessment(financing=upper).tests[2].verdict == FAIL
    brief = FinancingEvidence(50e6, 0.1, "brief line", exhaustive=True)
    assert _assessment(financing=brief).tests[2].verdict == INDETERMINATE


def test_atomic_and_warehouse_capital_can_cover_the_requirement_together():
    funds = FinancingEvidence(
        6e6, 30.0, "independent line", independent_of_atomic=True
    )
    test3 = _assessment(financing=funds).tests[2]
    assert test3.sub_outcomes[0].verdict == FAIL
    assert test3.sub_outcomes[1].verdict == INDETERMINATE
    assert test3.sub_outcomes[2].verdict == PASS
    assert test3.verdict == PASS
    schedules = next(
        e.value for e in test3.evidence
        if e.manifest == "derived_mixed_capacity"
    )
    for row in schedules.values():
        assert row["warehouse_capacity_at_bound_usd"] + 1e-6 >= (
            row["warehouse_seizure_usd"]
        )
    # Initial depth consumed by atomic clearing cannot fund warehouse exits.
    assert schedules["stressed DEX"]["warehouse_initial_capacity_usd"] == 0
    # Full-warehouse economics cannot certify a different funded route.
    result = _assessment(financing=funds)
    assert result.tests[3].verdict == INDETERMINATE
    assert result.tests[3].sub_outcomes[0].verdict == PASS
    assert result.overall == INDETERMINATE


def test_mixed_capital_requires_non_overlap_evidence():
    funds = FinancingEvidence(6e6, 30.0, "possibly shared liquidity")
    assert _assessment(financing=funds).tests[2].verdict == INDETERMINATE


def test_financing_rejects_invalid_inputs():
    for amount, days, source in [
        (-1, 2, "x"), (1, float("nan"), "x"), (1, 2, "")
    ]:
        try:
            FinancingEvidence(amount, days, source)
        except ValueError:
            continue
        raise AssertionError("invalid financing accepted")


def test_instant_clearance_passing_is_enough_for_test_three():
    def deep(m):
        m["market"]["clearance"]["max_clearable_usd_stressed"] = 1e9
        m["market"]["clearance"]["max_clearable_usd_quiet"] = 1e9

    test3 = _assessment(deep).tests[2]
    assert test3.sub_outcomes[0].verdict == PASS
    assert test3.verdict == PASS


# Test 4 -------------------------------------------------------------------


def _grid():
    inputs = _inputs()
    market = inputs["market"][1]
    sheet = reprice_balance_sheet(
        inputs["simultaneity"][1], inputs["balance_sheet"][1], 0.04
    )
    return sheet, market


def test_bonus_and_holding_times_are_repriced_at_the_p99_notional():
    result = _assessment()
    sheet = result.economics
    assert abs(sheet["debt_repaid_usd"] - 7146279.684218701) < 1e-6
    assert abs(sheet["seized_collateral_usd"] - 7575056.465271825) < 1e-6
    rows = _sweep(sheet)
    assert abs(rows["quiet DEX"]["minimum_bonus"] - 0.053454) < 1e-6
    assert abs(rows["stressed DEX"]["minimum_bonus"] - 0.054993) < 1e-6
    assert all(r["result"]["economic_clearance_pass"] for r in rows.values())
    assert result.tests[3].verdict == PASS
    assert rows["quiet DEX"]["result"]["time_to_clear_hours"] < 24
    assert rows["stressed DEX"]["result"]["first_cash_recovery_hours"] == 0
    assert "6.17 days" in result.tests[2].finding


def test_legacy_largest_borrower_results_are_not_reused():
    def corrupt(m):
        for row in _sweep(m["balance_sheet"]).values():
            row["minimum_bonus"] = 0.9
            row["result"]["time_to_clear_hours"] = 9999
    baseline = _assessment()
    modified = _assessment(corrupt)
    assert modified.economics == baseline.economics
    assert modified.tests == baseline.tests


def test_no_route_is_treated_as_evidenced():
    finding = _assessment().tests[3].finding
    assert "evidenced exit capacity" not in finding
    assert "stated sensitivity" in finding


def test_a_quiet_only_pass_is_not_a_pass():
    sheet, market = _grid()
    for name, entry in _sweep(sheet).items():
        passes = name == "quiet DEX"
        entry["minimum_bonus"] = 0.01 if passes else 0.5
        entry["result"]["economic_clearance_pass"] = passes
    assert assess_bonus_adequacy("derived", sheet, market).verdict == (
        INDETERMINATE
    )


def test_bonus_test_fails_when_no_regime_clears():
    sheet, market = _grid()
    for entry in _sweep(sheet).values():
        entry["minimum_bonus"] = 0.50
        entry["result"]["economic_clearance_pass"] = False
    assert assess_bonus_adequacy("derived", sheet, market).verdict == FAIL


def test_unresolved_economic_route_is_not_a_pass():
    sheet, market = _grid()
    row = _sweep(sheet)["stressed DEX"]
    row["minimum_bonus"] = None
    row["result"]["economic_clearance_pass"] = False
    assert assess_bonus_adequacy("derived", sheet, market).verdict == (
        INDETERMINATE
    )


def test_a_grid_without_an_adverse_regime_cannot_pass():
    sheet, market = _grid()
    for name in ("stressed DEX", "stressed + redemption"):
        del sheet["regimes"][name]
        del _sweep(sheet)[name]
    test4 = assess_bonus_adequacy("derived", sheet, market)
    assert test4.verdict == INDETERMINATE
    assert "no regime as adverse" in test4.finding


def test_a_flip_identifies_inputs_without_calling_them_measured():
    sheet, market = _grid()
    for name, entry in _sweep(sheet).items():
        passes = "redemption" in name
        entry["minimum_bonus"] = 0.01 if passes else 0.5
        entry["result"]["economic_clearance_pass"] = passes
    test4 = assess_bonus_adequacy("derived", sheet, market)
    assert test4.verdict == INDETERMINATE
    assert "redemption_capacity_usd_per_day" in test4.finding


def test_flipping_inputs_keeps_the_narrowest_difference():
    rows = {
        "a": {"inputs": {"x": 1, "y": 1}, "passes": False},
        "b": {"inputs": {"x": 1, "y": 2}, "passes": True},
        "c": {"inputs": {"x": 2, "y": 2}, "passes": True},
        "d": {"inputs": {"x": 2, "y": 1}, "passes": False},
    }
    assert flipping_inputs(rows) == ("y",)
    same = {k: {**v, "passes": True} for k, v in rows.items()}
    assert flipping_inputs(same) == ()


# Combination --------------------------------------------------------------


def test_notional_samples_report_signed_bonus_margins():
    diagnostic = _assessment().tests[3].diagnostics
    samples = diagnostic["bonus_notional_sensitivity"]["samples"]
    expected = {"p99": 50.0732, "tail_mean": -4.1367, "maximum": -831.5575}
    for name, margin in expected.items():
        row = samples[name]
        assert abs(row["bonus_margin_bps"] - margin) < 0.001
        assert row["worst_regime"] == "stressed DEX"
        assert row["all_regimes_cover"] == (name == "p99")


def test_notional_boundary_brackets_the_economic_profit_transition():
    result = _assessment()
    diagnostic = result.tests[3].diagnostics["bonus_notional_sensitivity"]
    boundary = diagnostic["coverage_boundary"]
    assert boundary["status"] == "bracketed"
    assert abs(boundary["repayment_usd"] - 22854470) < 2
    assert abs(boundary["multiple_of_p99"] - 3.1980934) < 1e-6
    assert abs(boundary["fraction_of_tail_mean"] - 0.9467093) < 1e-6
    low, high = boundary["covered_lower_usd"], boundary["uncovered_upper_usd"]
    assert 0 < high - low <= boundary["tolerance_usd"]
    sheet = result.economics
    assumptions = LiquidatorAssumptions(**sheet["common_assumptions"])
    for debt, covered in ((low, True), (high, False)):
        flags = []
        for params in sheet["regimes"].values():
            economics = simulate_liquidator_balance_sheet(
                debt, debt * (1 + sheet["liquidation_bonus"]),
                ExitAssumptions(**params), assumptions,
            )
            flags.append(economics.economic_clearance_pass)
        assert all(flags) == covered


def test_notional_boundary_and_tail_margin_respond_to_cost_assumptions():
    baseline = _assessment().tests[3].diagnostics["bonus_notional_sensitivity"]

    def lower_cost(m):
        m["balance_sheet"]["common_assumptions"]["dex_execution_loss"] = 0.009

    changed = _assessment(lower_cost).tests[3].diagnostics[
        "bonus_notional_sensitivity"
    ]
    assert changed["samples"]["tail_mean"]["all_regimes_cover"]
    assert changed["samples"]["tail_mean"]["bonus_margin_bps"] > 0
    assert changed["coverage_boundary"]["repayment_usd"] > (
        baseline["coverage_boundary"]["repayment_usd"]
    )


def test_notional_search_does_not_invent_an_unobserved_boundary():
    def deep(m):
        for params in m["balance_sheet"]["regimes"].values():
            params["instant_dex_capacity_usd"] = 1e12

    diagnostic = _assessment(deep).tests[3].diagnostics[
        "bonus_notional_sensitivity"
    ]
    assert diagnostic["coverage_boundary"]["status"] == "not_found_in_range"
    assert diagnostic["coverage_boundary"]["repayment_usd"] is None


def test_notional_search_distinguishes_uncovered_from_unresolved():
    def expensive(m):
        m["balance_sheet"]["common_assumptions"]["dex_execution_loss"] = 0.03

    def no_exit(m):
        costs = m["balance_sheet"]["common_assumptions"]
        costs["max_horizon_hours"] = 0.01

    for mutate, status in ((expensive, "not_covered_at_p99"),
                           (no_exit, "unresolved")):
        diagnostic = _assessment(mutate).tests[3].diagnostics[
            "bonus_notional_sensitivity"
        ]
        assert diagnostic["coverage_boundary"]["status"] == status
        assert diagnostic["coverage_boundary"]["repayment_usd"] is None


def test_documented_notional_limit_matches_the_calculated_result():
    limit = _assessment().tests[3].limits[0]
    paths = [
        "assessments/2026-08-18-aave-v3-ethereum-wsteth.md", "results.md"
    ]
    for name in paths:
        text = (_MANIFESTS.parent / name).read_text(encoding="utf-8")
        assert " ".join(limit.split()) in " ".join(text.split())


def test_combine_rules():
    assert combine((_outcome(1, PASS), _outcome(2, PASS)))[0] == PASS
    assert combine((_outcome(1, PASS), _outcome(2, INDETERMINATE)))[0] == (
        INDETERMINATE
    )
    assert combine((_outcome(1, FAIL), _outcome(2, PASS)))[0] == FAIL
    verdict, reason = combine((_outcome(1, INDETERMINATE),))
    assert verdict == INDETERMINATE and "never a pass" in reason
    _, reason = combine((_outcome(1, FAIL), _outcome(2, PASS)))
    assert "Test 2 remains" in reason


def test_a_failing_sub_outcome_is_reported_but_does_not_decide():
    sub = SubOutcome(
        label="3a x", scope="s", criterion="c", verdict=FAIL, finding="f"
    )
    verdict, reason = combine(
        (_outcome(1, PASS), _outcome(3, INDETERMINATE, subs=[sub]))
    )
    assert verdict == INDETERMINATE
    assert "3a x fails within its own scope" in reason


def test_mismatched_vintages_are_flagged():
    def shift(m):
        m["reachability"]["fixture"]["block"] = 1

    try:
        _assessment(shift)
    except ValueError as exc:
        assert "chain, asset or block" in str(exc)
    else:
        raise AssertionError("mixed-vintage assessment accepted")


def test_published_assessment_manifest_matches_the_rules():
    path = (
        _MANIFESTS / "ethereum-wsteth-four-test-assessment-25780402.json"
    )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["overall"] == INDETERMINATE
    assert manifest["scope"]["vintage_consistent"]
    assert [t["verdict"] for t in manifest["tests"]] == [
        PASS,
        PASS,
        INDETERMINATE,
        PASS,
    ]
    assert [s["verdict"] for s in manifest["tests"][2]["sub_outcomes"]] == [
        FAIL,
        INDETERMINATE,
        INDETERMINATE,
    ]
    assert manifest["derived_economics"] == _assessment().economics
    assert set(manifest["inputs"]) == set(_PATHS)
    inputs = {}
    for key, path in _PATHS.items():
        provenance = manifest["inputs"][key]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert provenance["sha256"] == digest
        inputs[key] = (provenance["file"], load_manifest(path))
    expected = build_assessment(
        inputs["reachability"], inputs["market"],
        inputs["simultaneity"], inputs["balance_sheet"],
        stress_path=manifest["scope"]["stress_path"],
        canonical_loss=manifest["scope"]["canonical_loss"],
    )
    # Normalize tuples to the JSON representation before comparing evidence.
    tests = json.loads(json.dumps([asdict(t) for t in expected.tests]))
    assert manifest["tests"] == tests
    assert manifest["overall_reason"] == expected.overall_reason


def _run_all():
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_")
    ]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} four-test assessment tests passed.")


if __name__ == "__main__":
    _run_all()
