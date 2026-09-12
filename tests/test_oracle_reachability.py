"""Tests for selector derivation, mock bytecode, and the oracle case."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from aave_risk_engine.data.abi import (
    decode_address,
    decode_int256,
    decode_string,
    decode_words,
    keccak256,
    selector,
)
from aave_risk_engine.data.evm_mock import (
    assemble, feed_bytecode, rate_bytecode,
)
from aave_risk_engine.oracle_reachability import (
    FAIL,
    INDETERMINATE,
    PASS,
    assess,
    cap_state,
    collect_bounds,
    expected_price,
    freshness,
    load_fixture,
    reconstruct_price,
    scenario_checks,
)

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES = {
    block: _ROOT / "data" / "oracle" / f"ethereum-wsteth-{block}.json"
    for block in (25_780_402, 25_946_216)
}


def _fixture(block: int = 25_780_402) -> dict:
    return load_fixture(_FIXTURES[block])


def _scenario(fixture: dict, name: str) -> dict:
    for scenario in fixture["behaviour"]["scenarios"]:
        if scenario["name"] == name:
            return scenario
    raise KeyError(name)


# ABI and mock instruments -------------------------------------------------


def test_keccak_matches_published_digests():
    assert keccak256(b"").hex() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )
    assert keccak256(b"abc").hex() == (
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    )


def test_selectors_match_the_values_already_used_in_the_repo():
    assert selector("decimals()") == "0x313ce567"
    assert selector("balanceOf(address)") == "0x70a08231"
    assert selector("getAssetPrice(address)") == "0xb3596f07"
    assert selector("getPriceOracle()") == "0xfca513a8"
    assert selector("getReserveConfigurationData(address)") == "0x3e150141"
    assert selector("latestRoundData()") == "0xfeaf968c"


def test_selector_rejects_a_non_signature():
    for bad in ("decimals", "decimals(", "()decimals"):
        try:
            selector(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_abi_decoders():
    assert decode_int256((1 << 256) - 1) == -1
    assert decode_address(0x1234) == "0x" + "0" * 36 + "1234"
    assert decode_words("0x" + "00" * 31 + "07") == [7]
    encoded = "0x" + f"{32:064x}" + f"{3:064x}" + "616263".ljust(64, "0")
    assert decode_string(encoded) == "abc"


def test_assembler_resolves_labels_and_rejects_overflow():
    code = assemble([("JUMP_TO", "end"), "JUMPI", ("LABEL", "end")])
    # PUSH2 0x0004, JUMPI, JUMPDEST at offset 4.
    assert code.hex() == "610004575b"
    for bad in ([("PUSH", 1, 256)], [("JUMP_TO", "nowhere")]):
        try:
            assemble(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_mock_bytecode_embeds_the_scenario_inputs():
    code = feed_bytecode(12345, 8, 1, 999, "fresh").removeprefix("0x")
    assert f"{12345:064x}" in code
    assert selector("latestRoundData()").removeprefix("0x") in code
    reverting = feed_bytecode(12345, 8, 1, 999, "revert").removeprefix("0x")
    # With reverting timestamp getters the updated_at value is never served.
    assert f"{999:064x}" not in reverting
    rate = rate_bytecode(
        "getPooledEthByShares(uint256)", 7, 18
    ).removeprefix("0x")
    assert selector("getPooledEthByShares(uint256)").removeprefix("0x") in rate


def test_every_override_was_verified_before_use():
    for block in _FIXTURES:
        for scenario in _fixture(block)["behaviour"]["scenarios"]:
            if scenario["overridden"] is not None:
                assert scenario["mock_verified"] is True, scenario["name"]


# Path reconstruction ------------------------------------------------------


def test_fixtures_are_pinned_and_self_describing():
    for block in _FIXTURES:
        fixture = _fixture(block)
        assert fixture["schema"] == "aave-oracle-reachability/2"
        assert fixture["block"] == block
        assert abs(fixture["reserve"]["liquidation_bonus"] - 0.06) < 1e-9


def test_reconstruction_matches_source_and_oracle_at_both_blocks():
    for block in _FIXTURES:
        result = reconstruct_price(_fixture(block))
        assert result.matches_source and result.matches_oracle, block
        assert result.error_raw == 0


def test_an_oracle_price_mismatch_alone_is_indeterminate():
    # Regression: the source matched while the oracle did not, and the
    # verdict used to stay PASS.
    fixture = copy.deepcopy(_fixture())
    fixture["aave_oracle"]["asset_price_raw"] += 1
    result = assess(fixture)
    assert reconstruct_price(fixture).matches_source
    assert not reconstruct_price(fixture).matches_oracle
    assert result.verdict == INDETERMINATE


def test_cap_parameters_are_internally_consistent():
    cap = cap_state(_fixture())
    assert cap.yearly_rate_consistent and cap.cap_flag_consistent
    assert not cap.reported_is_capped


def test_expected_price_applies_the_cap_and_declines_non_positive_inputs():
    fixture = _fixture()
    cap = cap_state(fixture)
    base = fixture["base_feed"]["supported"]["latestAnswer()"]
    above = cap.ceiling_ratio_raw * 2
    assert expected_price(fixture, base, above) == expected_price(
        fixture, base, cap.ceiling_ratio_raw
    )
    assert expected_price(fixture, 0, cap.current_ratio_raw) is None
    assert expected_price(fixture, -1, cap.current_ratio_raw) is None


# Behaviour of the deployed contracts --------------------------------------


def test_every_modelled_scenario_reproduces_exactly():
    for block in _FIXTURES:
        for check in scenario_checks(_fixture(block)):
            if check.has_expectation:
                assert check.matches, (block, check.name)


def test_stress_down_to_one_raw_unit_passes_through():
    for block in _FIXTURES:
        result = assess(_fixture(block))
        assert result.verdict == PASS
        assert result.max_verified_fall > 0.999999


def test_non_positive_values_are_refused_by_reverting():
    fixture = _fixture()
    for name in ("zero_answer", "negative_answer"):
        assert _scenario(fixture, name)["oracle"]["status"] == "reverted"
    assert any("rather than return zero" in c for c in assess(fixture).caveats)


def test_cap_clamps_a_rate_above_the_ceiling():
    fixture = _fixture()
    cap = cap_state(fixture)
    base = fixture["base_feed"]["supported"]["latestAnswer()"]
    observed = _scenario(fixture, "rate_above_cap")["oracle"]["price_raw"]
    assert observed == base * cap.ceiling_ratio_raw // 10**18


def test_freshness_properties_are_separate_and_behaviourally_settled():
    fixture = _fixture()
    fresh = freshness(fixture, scenario_checks(fixture))
    assert fresh.source_exposes_timestamp is False
    assert fresh.source_embeds_timestamp_call is False
    assert fresh.timestamp_getters_required_on_probed_path is False
    assert fresh.enforced is False
    assert fresh.stale_probe_age_seconds == 30 * 86_400


def test_reverting_getters_test_dependency_not_an_upstream_policy():
    fixture = copy.deepcopy(_fixture())
    scenario = _scenario(fixture, "timestamps_unreadable")
    scenario["oracle"] = {"status": "reverted", "price_raw": None}
    fresh = freshness(fixture, scenario_checks(fixture))
    assert fresh.timestamp_getters_required_on_probed_path is True
    # Reading a timestamp is not the same as enforcing a threshold.
    assert fresh.enforced is False
    stale = _scenario(fixture, "stale_30_days")
    stale["oracle"] = {"status": "reverted", "price_raw": None}
    assert freshness(fixture, scenario_checks(fixture)).enforced is True


def test_without_behaviour_nothing_is_verified():
    fixture = copy.deepcopy(_fixture())
    fixture["source"]["embedded_calls"] = {}
    del fixture["behaviour"]
    result = assess(fixture)
    assert result.verdict == INDETERMINATE
    assert result.freshness.timestamp_getters_required_on_probed_path is None
    assert result.freshness.enforced is None


def test_a_floored_stress_is_fail():
    fixture = copy.deepcopy(_fixture())
    scenario = _scenario(fixture, "stress_99")
    stress = _scenario(fixture, "stress_50")["oracle"]
    scenario["oracle"]["price_raw"] = stress["price_raw"]
    assert assess(fixture).verdict == FAIL


def test_a_refused_stress_is_fail():
    fixture = copy.deepcopy(_fixture())
    _scenario(fixture, "stress_50")["oracle"] = {
        "status": "reverted", "price_raw": None
    }
    assert assess(fixture).verdict == FAIL


def test_a_model_mismatch_outside_stress_is_indeterminate():
    fixture = copy.deepcopy(_fixture())
    _scenario(fixture, "recovery")["oracle"]["price_raw"] += 1
    result = assess(fixture)
    assert result.verdict == INDETERMINATE
    assert "recovery" in result.reasons[0]


def test_a_baseline_probe_that_disagrees_is_indeterminate():
    fixture = copy.deepcopy(_fixture())
    _scenario(fixture, "baseline")["oracle"]["price_raw"] += 1
    assert assess(fixture).verdict == INDETERMINATE


def test_only_the_aggregator_exposes_bounds():
    layers = {bound.layer for bound in collect_bounds(_fixture())}
    assert layers == {"base_feed.aggregator"}


def test_manifests_match_the_committed_fixtures():
    for block in _FIXTURES:
        path = (
            _ROOT
            / "docs"
            / "manifests"
            / f"ethereum-wsteth-oracle-reachability-{block}.json"
        )
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert manifest["fixture"]["block"] == block
        assert manifest["results"]["verdict"] == PASS
        assert manifest["results"]["reconstruction"]["matches_oracle"]
        assert manifest["results"]["freshness"]["enforced"] is False
        expected = assess(_fixture(block))
        assert manifest["results"]["reasons"] == list(expected.reasons)
        assert manifest["results"]["caveats"] == list(expected.caveats)
        digest = hashlib.sha256(_FIXTURES[block].read_bytes()).hexdigest()
        assert manifest["fixture"]["sha256"] == digest


def test_missing_stress_observation_cannot_pass():
    fixture = copy.deepcopy(_fixture())
    fixture["behaviour"]["scenarios"] = [
        s for s in fixture["behaviour"]["scenarios"]
        if s["name"] != "stress_floor"
    ]
    assert assess(fixture).verdict == INDETERMINATE


def test_unverified_override_cannot_pass():
    fixture = copy.deepcopy(_fixture())
    _scenario(fixture, "stress_50")["mock_verified"] = False
    assert assess(fixture).verdict == INDETERMINATE


def test_invalid_answer_probe_does_not_claim_a_rejected_transmission():
    result = assess(_fixture())
    caveats = " ".join(result.caveats)
    assert "not a rejected transmission" in caveats
    assert "refused only below" not in caveats
    assert result.exposed_minimum_raw == 1


def test_changed_timestamp_probe_price_is_not_claimed_unchanged():
    fixture = copy.deepcopy(_fixture())
    _scenario(fixture, "stale_30_days")["oracle"]["price_raw"] += 1
    assert assess(fixture).verdict == INDETERMINATE


def _run_all():
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_")
    ]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} oracle reachability tests passed.")


if __name__ == "__main__":
    _run_all()
