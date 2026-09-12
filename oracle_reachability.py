"""Offline analysis of whether a stress price can reach an Aave reserve.

Test 1 of the four-test sequence asks a question that precedes every
liquidation calculation: if the collateral moves, does the protocol see
it? A bound can stop a move from being published, and a path that never
reads a timestamp cannot reject a stale price.

Everything here runs against a pinned fixture built by
data.build_oracle_fixture. The fixture carries two kinds of evidence:
interface and bytecode observations of each layer, and behavioural
observations in which the deployed adapter and oracle were executed
against stated inputs through eth_call state overrides. Verdicts rest on
the behavioural observations. A path model that fails to reproduce any
observation is reported as unverified rather than tuned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "aave-oracle-reachability/2"
SECONDS_PER_YEAR = 31_536_000

PASS = "PASS"
FAIL = "FAIL"
INDETERMINATE = "INDETERMINATE"

_STRESS_SCENARIOS = ("stress_50", "stress_99", "stress_floor")
_REFUSAL_SCENARIOS = ("zero_answer", "negative_answer")
_FRESHNESS_SCENARIOS = ("timestamps_unreadable", "stale_30_days")


@dataclass(frozen=True)
class PriceReconstruction:
    """The baseline price recomputed from the layers beneath it."""

    base_answer_raw: int
    ratio_raw: int
    ratio_decimals: int
    capped_ratio_raw: int
    cap_applied: bool
    reconstructed_raw: int
    source_raw: int
    oracle_raw: int

    @property
    def matches_source(self) -> bool:
        return self.reconstructed_raw == self.source_raw

    @property
    def matches_oracle(self) -> bool:
        return self.reconstructed_raw == self.oracle_raw

    @property
    def matches_both(self) -> bool:
        return self.matches_source and self.matches_oracle

    @property
    def error_raw(self) -> int:
        return self.reconstructed_raw - self.source_raw


@dataclass(frozen=True)
class CapState:
    """The growth cap on the exchange rate, and whether it binds."""

    snapshot_ratio_raw: int
    snapshot_timestamp: int
    max_growth_per_second: int
    reported_yearly_percent: float
    derived_yearly_percent: float
    minimum_snapshot_delay_s: int
    elapsed_seconds: int
    ceiling_ratio_raw: int
    current_ratio_raw: int
    reported_is_capped: bool

    @property
    def headroom(self) -> float:
        return self.ceiling_ratio_raw / self.current_ratio_raw - 1.0

    @property
    def derived_is_capped(self) -> bool:
        return self.current_ratio_raw > self.ceiling_ratio_raw

    @property
    def yearly_rate_consistent(self) -> bool:
        difference = self.derived_yearly_percent - self.reported_yearly_percent
        return abs(difference) < 0.01

    @property
    def cap_flag_consistent(self) -> bool:
        return self.derived_is_capped == self.reported_is_capped


@dataclass(frozen=True)
class Bound:
    layer: str
    address: str
    kind: str
    raw_value: int


@dataclass(frozen=True)
class ScenarioCheck:
    """One behavioural observation compared with the path model."""

    name: str
    purpose: str
    observed_status: str
    observed_raw: int | None
    expected_raw: int | None

    @property
    def has_expectation(self) -> bool:
        return self.expected_raw is not None

    @property
    def matches(self) -> bool | None:
        if not self.has_expectation:
            return None
        return (self.observed_status == "ok"
                and self.observed_raw == self.expected_raw)


@dataclass(frozen=True)
class Freshness:
    """Interface evidence and dependency on timestamp getters in probes.

    A successful call with reverting getters establishes no dependency
    on those getters in that probe. It does not exclude caught reads or
    establish the replaced feed's own freshness policy.
    """

    source_exposes_timestamp: bool
    source_embeds_timestamp_call: bool
    timestamp_getters_required_on_probed_path: bool | None
    threshold_enforced_within_seconds: int | None
    stale_probe_age_seconds: int | None
    feed_age_seconds: int | None

    @property
    def enforced(self) -> bool | None:
        """Freshness rejection observed in probes, not a universal policy."""

        if self.timestamp_getters_required_on_probed_path is None:
            return None
        if self.threshold_enforced_within_seconds is not None:
            return True
        return False if self.stale_probe_age_seconds is not None else None


@dataclass(frozen=True)
class Assessment:
    asset: str
    chain: str
    block: int
    reconstruction: PriceReconstruction | None
    cap: CapState | None
    bounds: tuple[Bound, ...]
    scenarios: tuple[ScenarioCheck, ...]
    freshness: Freshness
    max_verified_fall: float | None
    exposed_minimum_raw: int | None
    verdict: str
    reasons: tuple[str, ...] = field(default_factory=tuple)
    caveats: tuple[str, ...] = field(default_factory=tuple)


def load_fixture(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        fixture = json.load(handle)
    if fixture.get("schema") != SCHEMA:
        raise ValueError(
            f"unexpected fixture schema: {fixture.get('schema')!r}"
        )
    return fixture


def _source(fixture: dict) -> dict:
    return fixture["source"]["supported"]


def cap_state(fixture: dict) -> CapState | None:
    source = _source(fixture)
    required = (
        "getSnapshotRatio()",
        "getSnapshotTimestamp()",
        "getMaxRatioGrowthPerSecond()",
        "getMaxYearlyGrowthRatePercent()",
    )
    ratio = fixture.get("ratio_provider") or {}
    if any(key not in source for key in required) or not ratio.get("resolved"):
        return None

    snapshot_ratio = source["getSnapshotRatio()"]
    snapshot_ts = source["getSnapshotTimestamp()"]
    growth = source["getMaxRatioGrowthPerSecond()"]
    elapsed = fixture["block_timestamp"] - snapshot_ts
    return CapState(
        snapshot_ratio_raw=snapshot_ratio,
        snapshot_timestamp=snapshot_ts,
        max_growth_per_second=growth,
        reported_yearly_percent=(
            source["getMaxYearlyGrowthRatePercent()"] / 100.0
        ),
        derived_yearly_percent=(
            growth * SECONDS_PER_YEAR / snapshot_ratio * 100.0
        ),
        minimum_snapshot_delay_s=source.get("MINIMUM_SNAPSHOT_DELAY()", 0),
        elapsed_seconds=elapsed,
        ceiling_ratio_raw=snapshot_ratio + growth * elapsed,
        current_ratio_raw=ratio["value_raw"],
        reported_is_capped=bool(source.get("isCapped()", False)),
    )


def expected_price(fixture: dict, base_answer: int, rate: int) -> int | None:
    """What the path model predicts for a given base answer and rate.

    Returns None for non-positive inputs, where the model makes no
    prediction and only the observation can say what happens.
    """

    source = _source(fixture)
    if "RATIO_DECIMALS()" not in source:
        return None
    cap = cap_state(fixture)
    used = min(rate, cap.ceiling_ratio_raw) if cap is not None else rate
    if base_answer <= 0 or used <= 0:
        return None
    return base_answer * used // (10 ** source["RATIO_DECIMALS()"])


def reconstruct_price(fixture: dict) -> PriceReconstruction | None:
    """Rebuild the baseline price and compare it with source and oracle."""

    source = _source(fixture)
    ratio = fixture.get("ratio_provider") or {}
    base = fixture.get("base_feed") or {}
    if not ratio.get("resolved") or "supported" not in base:
        return None
    if "RATIO_DECIMALS()" not in source:
        return None

    base_answer = base["supported"]["latestAnswer()"]
    ratio_raw = ratio["value_raw"]
    cap = cap_state(fixture)
    capped = min(ratio_raw, cap.ceiling_ratio_raw) if cap else ratio_raw
    return PriceReconstruction(
        base_answer_raw=base_answer,
        ratio_raw=ratio_raw,
        ratio_decimals=source["RATIO_DECIMALS()"],
        capped_ratio_raw=capped,
        cap_applied=capped < ratio_raw,
        reconstructed_raw=expected_price(fixture, base_answer, ratio_raw),
        source_raw=source["latestAnswer()"],
        oracle_raw=fixture["aave_oracle"]["asset_price_raw"],
    )


def collect_bounds(fixture: dict) -> tuple[Bound, ...]:
    """Every min/max answer bound exposed by a layer of the path."""

    bounds: list[Bound] = []
    layers = [("source", fixture["source"])]
    for key in ("base_feed", "asset_to_peg_feed", "peg_to_base_feed"):
        entry = fixture.get(key)
        if not entry:
            continue
        layers.append((key, entry))
        if "aggregator" in entry:
            layers.append((f"{key}.aggregator", entry["aggregator"]))
    for label, entry in layers:
        supported = entry.get("supported", {})
        for signature, kind in (
            ("minAnswer()", "min"), ("maxAnswer()", "max")
        ):
            if signature in supported:
                bounds.append(
                    Bound(label, entry["address"], kind, supported[signature])
                )
    return tuple(bounds)


def scenario_checks(fixture: dict) -> tuple[ScenarioCheck, ...]:
    behaviour = fixture.get("behaviour")
    if not behaviour:
        return ()
    base_answer = fixture["base_feed"]["supported"]["latestAnswer()"]
    rate = fixture["ratio_provider"]["value_raw"]
    checks = []
    for scenario in behaviour["scenarios"]:
        inputs = scenario["inputs"]
        scenario_base = inputs.get("base_answer_raw", base_answer)
        scenario_rate = inputs.get("rate_raw", rate)
        checks.append(
            ScenarioCheck(
                name=scenario["name"],
                purpose=scenario["purpose"],
                observed_status=scenario["oracle"]["status"],
                observed_raw=scenario["oracle"]["price_raw"],
                expected_raw=expected_price(
                    fixture, scenario_base, scenario_rate
                ),
            )
        )
    return tuple(checks)


def freshness(fixture: dict, checks: tuple[ScenarioCheck, ...]) -> Freshness:
    source = fixture["source"]
    embedded = source.get("embedded_calls", {})
    by_name = {check.name: check for check in checks}

    read_on_path: bool | None = None
    unreadable = by_name.get("timestamps_unreadable")
    if unreadable is not None:
        # This tests dependency, not whether a caught or ignored read occurred.
        if unreadable.observed_status == "reverted":
            read_on_path = True
        elif unreadable.matches:
            read_on_path = False

    enforced_within: int | None = None
    stale_age: int | None = None
    for scenario in (fixture.get("behaviour") or {}).get("scenarios", ()):
        if scenario["name"] == "stale_30_days":
            stale_age = (
                fixture["block_timestamp"] - scenario["inputs"]["updated_at"]
            )
            if scenario["oracle"]["status"] != "ok":
                enforced_within = stale_age

    feed_age = None
    round_data = fixture["base_feed"]["supported"].get("latestRoundData()")
    if isinstance(round_data, dict):
        feed_age = fixture["block_timestamp"] - round_data["updated_at"]

    return Freshness(
        source_exposes_timestamp=(
            "latestRoundData()" in source.get("supported", {})
        ),
        source_embeds_timestamp_call=any(
            embedded.get(sig, False)
            for sig in (
                "latestRoundData()",
                "latestTimestamp()",
                "latestRound()",
                "getRoundData(uint80)",
            )
        ),
        timestamp_getters_required_on_probed_path=read_on_path,
        threshold_enforced_within_seconds=enforced_within,
        stale_probe_age_seconds=stale_age,
        feed_age_seconds=feed_age,
    )


def _indeterminate(base: dict, reasons: list[str]) -> Assessment:
    return Assessment(**base, verdict=INDETERMINATE, reasons=tuple(reasons))


def assess(fixture: dict) -> Assessment:
    """Answer test 1 for this integration, with reasons and caveats."""

    reconstruction = reconstruct_price(fixture)
    cap = cap_state(fixture)
    checks = scenario_checks(fixture)
    fresh = freshness(fixture, checks) if "base_feed" in fixture else None
    by_name = {check.name: check for check in checks}

    floor_check = by_name.get("stress_floor")
    baseline = reconstruction.oracle_raw if reconstruction else None
    max_fall = (
        1.0 - floor_check.observed_raw / baseline
        if floor_check and floor_check.matches and baseline
        else None
    )
    aggregator_mins = [
        b.raw_value for b in collect_bounds(fixture) if b.kind == "min"
    ]
    base = dict(
        asset=fixture["asset"]["symbol"],
        chain=fixture["chain"],
        block=fixture["block"],
        reconstruction=reconstruction,
        cap=cap,
        bounds=collect_bounds(fixture),
        scenarios=checks,
        freshness=fresh,
        max_verified_fall=max_fall,
        exposed_minimum_raw=max(aggregator_mins) if aggregator_mins else None,
    )

    if reconstruction is None:
        return _indeterminate(
            base,
            [
                "the pricing formula for this source was not identified, so "
                "the path could not be reproduced from its inputs"
            ],
        )
    if not reconstruction.matches_both:
        return _indeterminate(
            base,
            [
                f"the reconstructed price {reconstruction.reconstructed_raw} "
                f"differs from the source ({reconstruction.source_raw}) "
                f"and the price AaveOracle reports "
                f"({reconstruction.oracle_raw}), so the protocol is not "
                "verified to read the modelled path"
            ],
        )
    required = (
        "baseline", *_STRESS_SCENARIOS, *_REFUSAL_SCENARIOS,
        *_FRESHNESS_SCENARIOS, "recovery", "rate_above_cap", "rate_drop_20",
    )
    observations = (fixture.get("behaviour") or {}).get("scenarios", ())
    missing = set(required) - set(by_name)
    unverified_mocks = [
        s["name"] for s in observations
        if (s.get("overridden") is not None
            and s.get("mock_verified") is not True)
    ]
    if not checks or missing or unverified_mocks:
        return _indeterminate(
            base,
            [
                "required observations or verified overrides are missing: "
                + ", ".join(sorted(missing) + unverified_mocks)
            ],
        )

    baseline_check = by_name.get("baseline")
    if (baseline_check is None
            or baseline_check.observed_raw != reconstruction.oracle_raw):
        return _indeterminate(
            base,
            [
                "the behavioural baseline does not reproduce the recorded "
                "oracle price, so the probes may not describe this state"
            ],
        )

    # Scenarios that test the model itself must reproduce exactly. A
    # mismatch means the model is wrong, not that the market is.
    model_checks = [
        check
        for check in checks
        if check.has_expectation
        and check.name not in _STRESS_SCENARIOS
        and check.name not in _FRESHNESS_SCENARIOS
    ]
    unverified = [check.name for check in model_checks if not check.matches]
    if unverified:
        return _indeterminate(
            base,
            [
                "the path model does not reproduce the behavioural "
                f"observations in {', '.join(unverified)}, so no conclusion "
                "about representability follows from it"
            ],
        )

    changed_freshness = [
        c.name for c in checks if c.name in _FRESHNESS_SCENARIOS
        and c.observed_status == "ok" and not c.matches
    ]
    if changed_freshness:
        return _indeterminate(
            base, ["timestamp probes changed the price: "
                   + ", ".join(changed_freshness)]
        )

    stress = [by_name[name] for name in _STRESS_SCENARIOS if name in by_name]
    if not stress:
        return _indeterminate(base, ["no stress scenario was observed"])
    blocked = [
        check
        for check in stress
        if check.observed_status != "ok"
        or (check.observed_raw or 0) > (check.expected_raw or 0)
    ]
    if blocked:
        names = ", ".join(check.name for check in blocked)
        return Assessment(
            **base,
            verdict=FAIL,
            reasons=(
                f"the deployed contracts refused or floored the stress in "
                f"{names}, so those inputs do not reach the consumer",
            ),
        )
    if not all(check.matches for check in stress):
        return _indeterminate(
            base,
            [
                "a stress observation differs from the model in a direction "
                "that is neither a pass-through nor a floor"
            ],
        )

    reasons = [
        "the baseline price is reproduced exactly from the base feed and "
        "the exchange rate, and matches both the source and AaveOracle",
        "the tested positive stress inputs, down to one raw unit of the "
        "base feed, passed through the deployed adapter and oracle exactly "
        "as modelled; the largest observed fall is nearly 100%, not zero "
        "price, conditional on upstream delivery",
    ]

    caveats: list[str] = []
    refusals = [by_name[n] for n in _REFUSAL_SCENARIOS if n in by_name]
    if refusals and all(c.observed_status == "reverted" for c in refusals):
        caveats.append(
            "a forced zero or negative feed answer makes getAssetPrice revert "
            "rather than return zero; this is an invalid-answer read probe, "
            "not a rejected transmission. Aggregator acceptance, storage "
            "after rejection, and publication liveness were not executed"
        )
    if fresh is not None:
        if fresh.timestamp_getters_required_on_probed_path is False:
            caveats.append(
                "the downstream read did not require the timestamp getters: "
                "both reverting getters and a round "
                f"{fresh.stale_probe_age_seconds:,} seconds old left its "
                "price unchanged. No freshness rejection was observed in "
                "these probes; this does not test the replaced feed's "
                "internal checks or exclude a caught timestamp read"
            )
        elif fresh.timestamp_getters_required_on_probed_path:
            caveats.append(
                "the downstream read depends on the probed timestamp interface"
                + (
                    " and refuses a round "
                    f"{fresh.threshold_enforced_within_seconds:,} "
                    "seconds old"
                    if fresh.threshold_enforced_within_seconds
                    else ", but did not refuse the stale round probed"
                )
            )
    if cap is not None:
        caveats.append(
            f"the growth cap limits the exchange rate to "
            f"{cap.reported_yearly_percent:.2f}% a year, with "
            f"{cap.headroom:.2%} of headroom at this block; a rate above the "
            "ceiling was clamped and a lower rate passed through, so the cap "
            "constrains upward moves in the rate and not a fall in price"
        )
    if not fixture["reserve"].get("borrowing_enabled", True):
        caveats.append(
            "borrowing this asset is disabled, so a stale mark would affect "
            "borrowing against it as collateral rather than borrowing it"
        )
    caveats.append(
        "downstream behaviour was verified for tested inputs at this block; "
        "a source replacement requires reassessment, and nothing here "
        "transfers to another source"
    )

    return Assessment(
        **base, verdict=PASS, reasons=tuple(reasons), caveats=tuple(caveats)
    )
