"""Offline analysis of whether a stress price can reach an Aave reserve.

Test 1 of the four-test sequence asks a question that precedes every
liquidation calculation: if the collateral moves, does the protocol see
it? A bounded feed can refuse to publish the move, and a price without a
timestamp cannot be rejected as stale.

Everything here runs against a pinned fixture built by
data.build_oracle_fixture. No network access, and no configurable
assumptions: the price reconstruction either reproduces the on-chain
answer or it does not, and a mismatch is reported rather than tuned away.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "aave-oracle-reachability/1"
SECONDS_PER_YEAR = 31_536_000


@dataclass(frozen=True)
class PriceReconstruction:
    """Recompute the source answer from the layers beneath it."""

    base_answer_raw: int
    ratio_raw: int
    ratio_decimals: int
    capped_ratio_raw: int
    cap_applied: bool
    reconstructed_raw: int
    reported_raw: int
    oracle_raw: int

    @property
    def matches_source(self) -> bool:
        return self.reconstructed_raw == self.reported_raw

    @property
    def matches_oracle(self) -> bool:
        return self.reported_raw == self.oracle_raw

    @property
    def error_raw(self) -> int:
        return self.reconstructed_raw - self.reported_raw


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
        return abs(self.derived_yearly_percent - self.reported_yearly_percent) < 0.01

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
class RepresentableRange:
    """How far the path can express a move before a bound stops it."""

    price_decimals: int
    current_price_raw: int
    floor_price_raw: int
    ceiling_price_raw: int | None
    binding_floor: Bound | None
    binding_ceiling: Bound | None
    bounds: tuple[Bound, ...]

    @property
    def max_representable_drawdown(self) -> float:
        return 1.0 - self.floor_price_raw / self.current_price_raw

    @property
    def floor_blocks_total_loss(self) -> bool:
        """True when the path cannot express a fall to near zero."""

        return self.max_representable_drawdown < 0.999


@dataclass(frozen=True)
class StalenessObservability:
    """Which timestamp, if any, a consumer of this path can inspect."""

    protocol_facing_layer: str
    protocol_facing_exposes_timestamp: bool
    protocol_facing_reverted: tuple[str, ...]
    deepest_timestamp: int | None
    deepest_timestamp_layer: str | None
    block_timestamp: int

    @property
    def age_seconds(self) -> int | None:
        if self.deepest_timestamp is None:
            return None
        return self.block_timestamp - self.deepest_timestamp

    @property
    def enforced_anywhere_on_path(self) -> bool:
        """A threshold can only be enforced where a timestamp is visible."""

        return self.protocol_facing_exposes_timestamp


@dataclass(frozen=True)
class Assessment:
    asset: str
    chain: str
    block: int
    reconstruction: PriceReconstruction | None
    cap: CapState | None
    representable: RepresentableRange
    staleness: StalenessObservability
    verdict: str
    reasons: tuple[str, ...] = field(default_factory=tuple)
    caveats: tuple[str, ...] = field(default_factory=tuple)


def load_fixture(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        fixture = json.load(handle)
    if fixture.get("schema") != SCHEMA:
        raise ValueError(f"unexpected fixture schema: {fixture.get('schema')!r}")
    return fixture


def _source(fixture: dict) -> dict:
    return fixture["source"]["supported"]


def reconstruct_price(fixture: dict) -> PriceReconstruction | None:
    """Rebuild the source answer from the base feed and the ratio.

    Returns None when the fixture does not describe a ratio-based
    adapter, rather than guessing at a different pricing formula.
    """

    source = _source(fixture)
    ratio = fixture.get("ratio_provider") or {}
    base = fixture.get("base_feed") or {}
    if not ratio.get("resolved") or "supported" not in base:
        return None
    if "RATIO_DECIMALS()" not in source:
        return None

    base_answer = base["supported"]["latestAnswer()"]
    ratio_raw = ratio["value_raw"]
    ratio_decimals = source["RATIO_DECIMALS()"]

    cap = cap_state(fixture)
    if cap is not None:
        capped_ratio = min(ratio_raw, cap.ceiling_ratio_raw)
    else:
        capped_ratio = ratio_raw

    reconstructed = base_answer * capped_ratio // (10**ratio_decimals)
    return PriceReconstruction(
        base_answer_raw=base_answer,
        ratio_raw=ratio_raw,
        ratio_decimals=ratio_decimals,
        capped_ratio_raw=capped_ratio,
        cap_applied=capped_ratio < ratio_raw,
        reconstructed_raw=reconstructed,
        reported_raw=source["latestAnswer()"],
        oracle_raw=fixture["aave_oracle"]["asset_price_raw"],
    )


def cap_state(fixture: dict) -> CapState | None:
    source = _source(fixture)
    required = (
        "getSnapshotRatio()",
        "getSnapshotTimestamp()",
        "getMaxRatioGrowthPerSecond()",
        "getMaxYearlyGrowthRatePercent()",
    )
    if any(key not in source for key in required):
        return None

    ratio = fixture.get("ratio_provider") or {}
    if not ratio.get("resolved"):
        return None

    snapshot_ratio = source["getSnapshotRatio()"]
    snapshot_ts = source["getSnapshotTimestamp()"]
    growth = source["getMaxRatioGrowthPerSecond()"]
    elapsed = fixture["block_timestamp"] - snapshot_ts
    return CapState(
        snapshot_ratio_raw=snapshot_ratio,
        snapshot_timestamp=snapshot_ts,
        max_growth_per_second=growth,
        reported_yearly_percent=source["getMaxYearlyGrowthRatePercent()"] / 100.0,
        derived_yearly_percent=growth * SECONDS_PER_YEAR / snapshot_ratio * 100.0,
        minimum_snapshot_delay_s=source.get("MINIMUM_SNAPSHOT_DELAY()", 0),
        elapsed_seconds=elapsed,
        ceiling_ratio_raw=snapshot_ratio + growth * elapsed,
        current_ratio_raw=ratio["value_raw"],
        reported_is_capped=bool(source.get("isCapped()", False)),
    )


def collect_bounds(fixture: dict) -> tuple[Bound, ...]:
    """Every min/max answer bound actually present on the path."""

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
        for signature, kind in (("minAnswer()", "min"), ("maxAnswer()", "max")):
            if signature in supported:
                bounds.append(
                    Bound(
                        layer=label,
                        address=entry["address"],
                        kind=kind,
                        raw_value=supported[signature],
                    )
                )
    return tuple(bounds)


def representable_range(fixture: dict) -> RepresentableRange:
    """Translate the bounds on the path into a range of expressible prices.

    A bound sits on a feed beneath the adapter, so it is translated into
    the reserve's price units through the same ratio the adapter applies.
    """

    source = _source(fixture)
    bounds = collect_bounds(fixture)
    current = source["latestAnswer()"]
    decimals = source["decimals()"]

    reconstruction = reconstruct_price(fixture)
    if reconstruction is not None:
        scale = reconstruction.capped_ratio_raw / (
            10**reconstruction.ratio_decimals
        )
    else:
        scale = 1.0

    floors = [
        (b.raw_value * scale, b) for b in bounds if b.kind == "min"
    ]
    ceilings = [
        (b.raw_value * scale, b) for b in bounds if b.kind == "max"
    ]
    floor_value, floor_bound = max(floors, default=(0.0, None))
    ceiling_value, ceiling_bound = min(ceilings, default=(None, None))

    return RepresentableRange(
        price_decimals=decimals,
        current_price_raw=current,
        floor_price_raw=int(floor_value),
        ceiling_price_raw=int(ceiling_value) if ceiling_value is not None else None,
        binding_floor=floor_bound,
        binding_ceiling=ceiling_bound,
        bounds=bounds,
    )


def staleness_observability(fixture: dict) -> StalenessObservability:
    source_entry = fixture["source"]
    exposes = "latestRoundData()" in source_entry.get("supported", {})

    deepest_ts: int | None = None
    deepest_layer: str | None = None
    for key in ("base_feed", "asset_to_peg_feed", "peg_to_base_feed"):
        entry = fixture.get(key)
        if not entry:
            continue
        for label, node in (
            (key, entry),
            (f"{key}.aggregator", entry.get("aggregator") or {}),
        ):
            supported = node.get("supported", {})
            round_data = supported.get("latestRoundData()")
            candidate = None
            if isinstance(round_data, dict):
                candidate = round_data.get("updated_at")
            elif "latestTimestamp()" in supported:
                candidate = supported["latestTimestamp()"]
            if candidate and (deepest_ts is None or candidate > deepest_ts):
                deepest_ts, deepest_layer = candidate, label

    return StalenessObservability(
        protocol_facing_layer=source_entry["address"],
        protocol_facing_exposes_timestamp=exposes,
        protocol_facing_reverted=tuple(source_entry.get("reverted", ())),
        deepest_timestamp=deepest_ts,
        deepest_timestamp_layer=deepest_layer,
        block_timestamp=fixture["block_timestamp"],
    )


def assess(fixture: dict) -> Assessment:
    """Answer test 1 for this integration, with reasons and caveats."""

    reconstruction = reconstruct_price(fixture)
    cap = cap_state(fixture)
    representable = representable_range(fixture)
    stale = staleness_observability(fixture)

    reasons: list[str] = []
    caveats: list[str] = []

    if reconstruction is None:
        verdict = "INDETERMINATE"
        reasons.append(
            "the pricing formula for this source was not identified, so the "
            "path could not be reproduced from its inputs"
        )
        return Assessment(
            asset=fixture["asset"]["symbol"],
            chain=fixture["chain"],
            block=fixture["block"],
            reconstruction=None,
            cap=cap,
            representable=representable,
            staleness=stale,
            verdict=verdict,
            reasons=tuple(reasons),
        )

    if not reconstruction.matches_source:
        verdict = "INDETERMINATE"
        reasons.append(
            f"the reconstructed answer differs from the reported one by "
            f"{reconstruction.error_raw} raw units, so the model of this "
            "path is not verified and no conclusion follows from it"
        )
        return Assessment(
            asset=fixture["asset"]["symbol"],
            chain=fixture["chain"],
            block=fixture["block"],
            reconstruction=reconstruction,
            cap=cap,
            representable=representable,
            staleness=stale,
            verdict=verdict,
            reasons=tuple(reasons),
        )

    reasons.append(
        "the source answer is reproduced exactly from the base feed and the "
        "exchange rate, so the path is modelled, not assumed"
    )

    if representable.floor_blocks_total_loss:
        verdict = "FAIL"
        reasons.append(
            f"a bound at {representable.binding_floor.layer} stops the path "
            f"from expressing a fall beyond "
            f"{representable.max_representable_drawdown:.2%}"
        )
    else:
        verdict = "PASS"
        reasons.append(
            "no bound on the path prevents a fall of arbitrary size from "
            "being published, so a stress mark is representable"
        )

    if cap is not None:
        if not cap.yearly_rate_consistent or not cap.cap_flag_consistent:
            caveats.append(
                "the cap parameters are not internally consistent with the "
                "reported cap state; treat the cap analysis as unverified"
            )
        else:
            caveats.append(
                f"the growth cap limits the exchange rate to "
                f"{cap.reported_yearly_percent:.2f}% a year and currently has "
                f"{cap.headroom:.2%} of headroom; it is a ceiling on the "
                "ratio and does not constrain a fall in price"
            )

    if not stale.enforced_anywhere_on_path:
        caveats.append(
            "the protocol-facing layer exposes no timestamp, so no staleness "
            "threshold can be enforced against this price in code; freshness "
            "is an operational assumption, not an enforced condition"
        )
        if stale.age_seconds is not None:
            caveats.append(
                f"the deepest timestamp available is at "
                f"{stale.deepest_timestamp_layer}, {stale.age_seconds} seconds "
                "before the pinned block, and is not read by this path"
            )

    if not fixture["reserve"].get("borrowing_enabled", True):
        caveats.append(
            "borrowing this asset is disabled, so a stale mark affects "
            "borrowing against it as collateral rather than borrowing it"
        )

    return Assessment(
        asset=fixture["asset"]["symbol"],
        chain=fixture["chain"],
        block=fixture["block"],
        reconstruction=reconstruction,
        cap=cap,
        representable=representable,
        staleness=stale,
        verdict=verdict,
        reasons=tuple(reasons),
        caveats=tuple(caveats),
    )
