"""Tests for selector derivation and the pinned oracle-reachability case."""

from __future__ import annotations

import copy
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
from aave_risk_engine.oracle_reachability import (
    assess,
    cap_state,
    collect_bounds,
    load_fixture,
    reconstruct_price,
    representable_range,
    staleness_observability,
)

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "oracle"
    / "ethereum-wsteth-25946216.json"
)


def _fixture() -> dict:
    return load_fixture(_FIXTURE)


def test_keccak_matches_published_digest():
    assert keccak256(b"").hex() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )
    assert keccak256(b"abc").hex() == (
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    )


def test_selectors_match_the_values_already_used_in_the_repo():
    # These four are independently hardcoded in data/aave_v3.py, so the
    # derivation is checked against values the engine already relies on.
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
    word = (1 << 256) - 1
    assert decode_int256(word) == -1
    assert decode_address(0x1234) == "0x" + "0" * 36 + "1234"
    assert decode_words("0x" + "00" * 31 + "07") == [7]
    encoded = (
        "0x"
        + f"{32:064x}"
        + f"{3:064x}"
        + "616263".ljust(64, "0")
    )
    assert decode_string(encoded) == "abc"


def test_fixture_is_pinned_and_self_describing():
    fixture = _fixture()
    assert fixture["schema"] == "aave-oracle-reachability/1"
    assert fixture["chain"] == "ethereum"
    assert fixture["block"] == 25_946_216
    assert fixture["asset"]["symbol"] == "wstETH"
    # The reserve parameters must match the ones the published liquidator
    # balance sheet uses, otherwise the two analyses describe different books.
    assert abs(fixture["reserve"]["liquidation_bonus"] - 0.06) < 1e-9
    assert abs(fixture["reserve"]["liquidation_threshold"] - 0.81) < 1e-9


def test_price_reconstruction_reproduces_the_onchain_answer():
    result = reconstruct_price(_fixture())
    assert result is not None
    assert result.error_raw == 0
    assert result.matches_source
    assert result.matches_oracle
    assert result.reconstructed_raw == 307_999_973_407


def test_cap_parameters_are_internally_consistent():
    cap = cap_state(_fixture())
    assert cap is not None
    assert cap.yearly_rate_consistent
    assert cap.cap_flag_consistent
    assert not cap.reported_is_capped
    assert 0.0 < cap.headroom < 0.05


def test_only_the_aggregator_carries_bounds():
    bounds = collect_bounds(_fixture())
    layers = {bound.layer for bound in bounds}
    assert layers == {"base_feed.aggregator"}
    # The Aave-facing source refuses both bound getters, so it imposes none.
    reverted = set(_fixture()["source"]["reverted"])
    assert {"minAnswer()", "maxAnswer()"} <= reverted


def test_the_path_can_express_an_arbitrary_fall():
    span = representable_range(_fixture())
    assert span.max_representable_drawdown > 0.999
    assert not span.floor_blocks_total_loss


def test_the_protocol_facing_layer_exposes_no_timestamp():
    stale = staleness_observability(_fixture())
    assert not stale.protocol_facing_exposes_timestamp
    assert "latestRoundData()" in stale.protocol_facing_reverted
    assert not stale.enforced_anywhere_on_path
    # A timestamp exists further down, but nothing on the path reads it.
    assert stale.deepest_timestamp is not None
    assert stale.age_seconds > 0


def test_verdict_is_pass_with_the_staleness_caveat():
    result = assess(_fixture())
    assert result.verdict == "PASS"
    assert any("no bound on the path" in reason for reason in result.reasons)
    assert any("no staleness threshold" in caveat for caveat in result.caveats)
    assert any("does not constrain a fall" in caveat for caveat in result.caveats)


def test_a_binding_floor_turns_the_verdict_into_fail():
    fixture = copy.deepcopy(_fixture())
    # Raise the aggregator floor to 10% below the current base answer.
    current = fixture["base_feed"]["supported"]["latestAnswer()"]
    fixture["base_feed"]["aggregator"]["supported"]["minAnswer()"] = int(
        current * 0.9
    )
    result = assess(fixture)
    assert result.verdict == "FAIL"
    assert any("stops the path" in reason for reason in result.reasons)
    assert result.representable.max_representable_drawdown < 0.11


def test_a_reconstruction_mismatch_is_indeterminate_not_pass():
    fixture = copy.deepcopy(_fixture())
    fixture["source"]["supported"]["latestAnswer()"] += 1
    result = assess(fixture)
    assert result.verdict == "INDETERMINATE"
    assert any("not verified" in reason for reason in result.reasons)


def test_an_unidentified_pricing_formula_is_indeterminate():
    fixture = copy.deepcopy(_fixture())
    fixture["ratio_provider"]["resolved"] = False
    result = assess(fixture)
    assert result.verdict == "INDETERMINATE"
    assert any("not identified" in reason for reason in result.reasons)


def test_manifest_matches_the_committed_fixture():
    manifest_path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "manifests"
        / "ethereum-wsteth-oracle-reachability-25946216.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["fixture"]["block"] == 25_946_216
    assert manifest["results"]["verdict"] == "PASS"
    assert manifest["results"]["reconstruction"]["error_raw"] == 0
    assert not manifest["results"]["staleness"][
        "protocol_facing_exposes_timestamp"
    ]


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
