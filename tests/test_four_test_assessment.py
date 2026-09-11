"""Tests for the four-test assessment rules and the worked wstETH case."""

from __future__ import annotations

import copy
import json
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
)

_MANIFESTS = Path(__file__).resolve().parents[1] / "docs" / "manifests"
_PATHS = {
    "reachability": _MANIFESTS / "ethereum-wsteth-oracle-reachability-25780402.json",
    "market": _MANIFESTS / "ethereum-wsteth-2026-08-18.json",
    "simultaneity": _MANIFESTS
    / "ethereum-wsteth-simultaneous-requirement-2026-08-18.json",
    "balance_sheet": _MANIFESTS
    / "ethereum-wsteth-liquidator-balance-sheet-2026-08-18.json",
}
_STRESS = "pinned market report stress path"


def _inputs() -> dict:
    return {key: (key, copy.deepcopy(load_manifest(p))) for key, p in _PATHS.items()}


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
        INDETERMINATE,
    ]
    assert result.overall == INDETERMINATE
    assert "3a instant clearance fails within its own scope" in result.overall_reason


def test_every_verdict_cites_at_least_one_manifest_field():
    for test in _assessment().tests:
        assert test.evidence, f"test {test.number} cites no evidence"


# Test 2 -------------------------------------------------------------------


def test_requirement_comes_from_the_stress_path_not_the_static_bound():
    test2 = _assessment().tests[1]
    assert "$7.15m of debt repayment" in test2.finding
    assert "$7.58m of collateral" in test2.finding
    assert "conservative bound, not the tail requirement" in test2.finding


def test_requirement_distinguishes_debt_from_collateral():
    finding = _assessment().tests[1].finding
    assert "of debt repayment" in finding
    assert "of collateral" in finding
    # No aggregate book debt is compared with a collateral figure.
    assert "combined book" not in finding


def test_requirement_accounts_for_capital_recycling():
    assert "fastest modelled exit takes 11.26 days" in _assessment().tests[1].finding


def test_provenance_alone_no_longer_passes_test_two():
    def unreproduced(m):
        m["simultaneity"]["stress_path"]["reproduces_published_scenarios"] = False

    assert _assessment(unreproduced).tests[1].verdict == INDETERMINATE

    def incomplete(m):
        m["market"]["snapshot"]["borrower_discovery"]["source"] = "sampled accounts"

    assert _assessment(incomplete).tests[1].verdict == INDETERMINATE


# Test 3 -------------------------------------------------------------------


def test_instant_clearance_fails_without_deciding_financing():
    test3 = _assessment().tests[2]
    instant, warehouse = test3.sub_outcomes
    assert instant.verdict == FAIL
    assert warehouse.verdict == INDETERMINATE
    assert test3.verdict == INDETERMINATE
    assert "does not show that nobody can finance" in test3.finding


def test_evidenced_financing_resolves_test_three_either_way():
    enough = FinancingEvidence(
        committed_usd=50e6, holding_days=30.0, source="stated credit line"
    )
    assert _assessment(financing=enough).tests[2].verdict == PASS

    short = FinancingEvidence(committed_usd=1e6, holding_days=30.0, source="x")
    result = _assessment(financing=short)
    assert result.tests[2].verdict == FAIL
    assert result.overall == FAIL

    brief = FinancingEvidence(committed_usd=50e6, holding_days=2.0, source="x")
    assert _assessment(financing=brief).tests[2].verdict == FAIL


def test_instant_clearance_passing_is_enough_for_test_three():
    def deep(m):
        m["market"]["clearance"]["max_clearable_usd_stressed"] = 1e9
        m["market"]["clearance"]["max_clearable_usd_quiet"] = 1e9

    test3 = _assessment(deep).tests[2]
    assert test3.sub_outcomes[0].verdict == PASS
    assert test3.verdict == PASS


# Test 4 -------------------------------------------------------------------


def test_bonus_test_names_the_input_that_flips_it():
    test4 = _assessment().tests[3]
    assert test4.verdict == INDETERMINATE
    assert "redemption_capacity_usd_per_day" in test4.finding
    assert "DEX refill times" in test4.finding
    assert test4.missing and test4.who_can_supply


def test_no_route_is_treated_as_evidenced():
    finding = _assessment().tests[3].finding
    assert "evidenced exit capacity" not in finding
    assert "stated sensitivity rather than a measurement" in finding


def test_a_quiet_only_pass_is_not_a_pass():
    # Regression: any passing DEX route used to be enough.
    def quiet_clears(m):
        _sweep(m["balance_sheet"])["quiet DEX"]["minimum_bonus"] = 0.01
        _sweep(m["balance_sheet"])["quiet DEX"]["result"][
            "economic_clearance_pass"
        ] = True

    assert _assessment(quiet_clears).tests[3].verdict == INDETERMINATE


def test_bonus_test_passes_only_when_every_regime_clears():
    def all_clear(m):
        for entry in _sweep(m["balance_sheet"]).values():
            entry["minimum_bonus"] = 0.01
            entry["result"]["economic_clearance_pass"] = True

    assert _assessment(all_clear).tests[3].verdict == PASS


def test_bonus_test_fails_when_no_regime_clears():
    def none_clear(m):
        for entry in _sweep(m["balance_sheet"]).values():
            entry["minimum_bonus"] = 0.50
            entry["result"]["economic_clearance_pass"] = False

    assert _assessment(none_clear).tests[3].verdict == FAIL


def test_a_grid_without_an_adverse_regime_cannot_pass():
    def benign(m):
        sheet = m["balance_sheet"]
        for name in ("stressed DEX", "stressed + redemption"):
            del sheet["regimes"][name]
            del _sweep(sheet)[name]
        for entry in _sweep(sheet).values():
            entry["minimum_bonus"] = 0.01
            entry["result"]["economic_clearance_pass"] = True

    test4 = _assessment(benign).tests[3]
    assert test4.verdict == INDETERMINATE
    assert "no regime as adverse" in test4.finding


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
    sub = SubOutcome(label="3a x", scope="s", criterion="c", verdict=FAIL, finding="f")
    verdict, reason = combine(
        (_outcome(1, PASS), _outcome(3, INDETERMINATE, subs=[sub]))
    )
    assert verdict == INDETERMINATE
    assert "3a x fails within its own scope" in reason


def test_mismatched_vintages_are_flagged():
    def shift(m):
        m["reachability"]["fixture"]["block"] = 1

    assert not _assessment(shift).vintage_consistent


def test_published_assessment_manifest_matches_the_rules():
    manifest = json.loads(
        (_MANIFESTS / "ethereum-wsteth-four-test-assessment-25780402.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["overall"] == INDETERMINATE
    assert manifest["scope"]["vintage_consistent"]
    assert [t["verdict"] for t in manifest["tests"]] == [
        PASS,
        PASS,
        INDETERMINATE,
        INDETERMINATE,
    ]
    assert [s["verdict"] for s in manifest["tests"][2]["sub_outcomes"]] == [
        FAIL,
        INDETERMINATE,
    ]
    assert set(manifest["inputs"]) == set(_PATHS)


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
