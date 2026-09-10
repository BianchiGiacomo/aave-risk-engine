"""Tests for the four-test assessment rules and the worked wstETH case."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from aave_risk_engine.four_test_assessment import (
    FAIL,
    INDETERMINATE,
    PASS,
    TestOutcome,
    build_assessment,
    combine,
    load_manifest,
)

_MANIFESTS = Path(__file__).resolve().parents[1] / "docs" / "manifests"
_REACHABILITY = _MANIFESTS / "ethereum-wsteth-oracle-reachability-25780402.json"
_MARKET = _MANIFESTS / "ethereum-wsteth-2026-08-18.json"
_BALANCE_SHEET = (
    _MANIFESTS / "ethereum-wsteth-liquidator-balance-sheet-2026-08-18.json"
)
_STRESS = "pinned market report stress path"


def _inputs():
    return (
        ("reachability", load_manifest(_REACHABILITY)),
        ("market", load_manifest(_MARKET)),
        ("balance_sheet", load_manifest(_BALANCE_SHEET)),
    )


def _assessment(overrides=None):
    reach, market, sheet = _inputs()
    if overrides:
        reach = (reach[0], copy.deepcopy(reach[1]))
        market = (market[0], copy.deepcopy(market[1]))
        sheet = (sheet[0], copy.deepcopy(sheet[1]))
        overrides(reach[1], market[1], sheet[1])
    return build_assessment(reach, market, sheet, stress_path=_STRESS)


def _outcome(number: int, verdict: str) -> TestOutcome:
    return TestOutcome(
        number=number, question="q", criterion="c", verdict=verdict, finding="f"
    )


def test_all_three_manifests_share_one_block():
    result = _assessment()
    assert result.vintage_consistent
    assert result.block == 25_780_402


def test_worked_case_verdicts():
    result = _assessment()
    assert [test.verdict for test in result.tests] == [
        PASS,
        PASS,
        FAIL,
        INDETERMINATE,
    ]
    assert result.overall == FAIL
    assert "test 3 fails" in result.overall_reason


def test_failing_test_three_dominates_a_later_indeterminate():
    result = _assessment()
    # Test 4 is unresolved, but the overall verdict is driven by test 3.
    assert result.tests[3].verdict == INDETERMINATE
    assert result.overall == FAIL
    assert "conditional" in result.overall_reason


def test_capacity_shortfall_is_reported_against_the_requirement():
    result = _assessment()
    finding = result.tests[2].finding
    assert "$2.73m quiet" in finding
    assert "$256.52m single-event requirement" in finding


def test_bonus_test_is_indeterminate_because_redemption_is_assumed():
    outcome = _assessment().tests[3]
    assert outcome.verdict == INDETERMINATE
    assert "not a measured entitlement" in outcome.finding
    assert outcome.missing
    assert outcome.who_can_supply


def test_bonus_test_passes_when_a_dex_only_route_clears():
    def bump(_reach, _market, sheet):
        for entry in sheet["canonical_loss_sweep"]["0.040000"].values():
            if entry["result"]["redemption_exit_usd"] == 0.0:
                entry["result"]["economic_clearance_pass"] = True

    outcome = _assessment(bump).tests[3]
    assert outcome.verdict == PASS
    assert not outcome.missing


def test_incomplete_registry_makes_the_requirement_indeterminate():
    def degrade(_reach, market, _sheet):
        market["snapshot"]["borrower_discovery"]["source"] = "sampled accounts"

    outcome = _assessment(degrade).tests[1]
    assert outcome.verdict == INDETERMINATE
    assert "not established" in outcome.finding


def test_a_reachability_failure_propagates_to_the_overall_verdict():
    def bound(reach, _market, _sheet):
        reach["results"]["verdict"] = FAIL
        reach["results"]["max_representable_drawdown"] = 0.08

    result = _assessment(bound)
    assert result.tests[0].verdict == FAIL
    assert result.overall == FAIL
    assert "test 1 fails" in result.overall_reason


def test_every_verdict_cites_at_least_one_manifest_field():
    for test in _assessment().tests:
        assert test.evidence, f"test {test.number} cites no evidence"
        for item in test.evidence:
            assert item.manifest
            assert item.field


def test_combine_rules():
    assert combine((_outcome(1, PASS), _outcome(2, PASS)))[0] == PASS
    assert combine((_outcome(1, PASS), _outcome(2, INDETERMINATE)))[0] == (
        INDETERMINATE
    )
    assert combine((_outcome(1, FAIL), _outcome(2, PASS)))[0] == FAIL
    # Missing data is never a pass, even when nothing fails outright.
    verdict, reason = combine((_outcome(1, INDETERMINATE),))
    assert verdict == INDETERMINATE
    assert "never a pass" in reason
    # One later test reads as singular.
    _, reason = combine((_outcome(1, FAIL), _outcome(2, PASS)))
    assert "Test 2 remains" in reason


def test_mismatched_vintages_are_flagged():
    def shift(reach, _market, _sheet):
        reach["fixture"]["block"] = 1

    assert not _assessment(shift).vintage_consistent


def test_published_assessment_manifest_matches_the_rules():
    manifest = json.loads(
        (_MANIFESTS / "ethereum-wsteth-four-test-assessment-25780402.json")
        .read_text(encoding="utf-8")
    )
    assert manifest["overall"] == FAIL
    assert manifest["scope"]["vintage_consistent"]
    assert manifest["scope"]["block"] == 25_780_402
    assert [test["verdict"] for test in manifest["tests"]] == [
        PASS,
        PASS,
        FAIL,
        INDETERMINATE,
    ]
    assert manifest["rules"]["missing_data"] == "missing data is never a PASS"


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
