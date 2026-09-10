"""Assemble a four-test assessment for one reserve from pinned manifests.

The four tests run in order, and the order matters:

  1. can the oracle represent the stress and transmit it to the protocol;
  2. how much debt requires repayment simultaneously, or before capital
     from earlier liquidations becomes available again;
  3. can eligible liquidators finance that repayment when required;
  4. does the bonus compensate for settlement and recovery risk.

Every verdict here is derived from a committed manifest field rather than
written by hand, and every verdict carries the field it rests on. An
outcome is PASS only when the stated criterion is met on the evidence,
FAIL when it is violated, and INDETERMINATE when the available evidence
cannot support either. Missing data is never a PASS.

Later tests stay meaningful when an earlier one is unresolved, but they
remain conditional: they cannot establish overall clearance on their own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

PASS = "PASS"
FAIL = "FAIL"
INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True)
class Evidence:
    """One manifest field a verdict rests on."""

    manifest: str
    field: str
    value: object


@dataclass(frozen=True)
class TestOutcome:
    number: int
    question: str
    criterion: str
    verdict: str
    finding: str
    evidence: tuple[Evidence, ...] = ()
    missing: tuple[str, ...] = ()
    who_can_supply: tuple[str, ...] = ()


@dataclass(frozen=True)
class Assessment:
    chain: str
    asset: str
    block: int
    block_timestamp: int
    stress_path: str
    tests: tuple[TestOutcome, ...]
    overall: str
    overall_reason: str
    vintage_consistent: bool


def load_manifest(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _dig(payload: dict, dotted: str):
    node = payload
    for part in dotted.split("."):
        node = node[part]
    return node


def _evidence(name: str, payload: dict, *fields: str) -> tuple[Evidence, ...]:
    return tuple(
        Evidence(manifest=name, field=field, value=_dig(payload, field))
        for field in fields
    )


def assess_oracle_reachability(name: str, manifest: dict) -> TestOutcome:
    verdict = _dig(manifest, "results.verdict")
    exposes = _dig(
        manifest, "results.staleness.protocol_facing_exposes_timestamp"
    )
    drawdown = _dig(manifest, "results.max_representable_drawdown")
    error = _dig(manifest, "results.reconstruction.error_raw")

    if error != 0:
        finding = (
            "the price path could not be reproduced from its inputs, so no "
            "conclusion about representability follows"
        )
    elif verdict == PASS:
        finding = (
            f"the path reproduces the chain exactly and no bound stops a "
            f"fall of up to {drawdown:.2%}, so a stress mark is "
            "representable. Freshness, however, is not enforced in code: "
            "the layer the protocol reads exposes no timestamp"
            if not exposes
            else f"a fall of up to {drawdown:.2%} is representable"
        )
    else:
        finding = "a bound on the path prevents the stress from being published"

    return TestOutcome(
        number=1,
        question=(
            "can the oracle represent the stress and transmit it to the "
            "protocol"
        ),
        criterion=(
            "the price path reproduces the chain exactly, and no bound on "
            "it prevents the stress mark from being published"
        ),
        verdict=verdict,
        finding=finding,
        evidence=_evidence(
            name,
            manifest,
            "results.verdict",
            "results.reconstruction.error_raw",
            "results.max_representable_drawdown",
            "results.staleness.protocol_facing_exposes_timestamp",
        ),
        missing=(
            ()
            if exposes
            else (
                "an off-chain publication guarantee for the underlying feed, "
                "since no on-chain freshness check is possible on this path",
            )
        ),
        who_can_supply=() if exposes else ("the feed operator", "Aave governance"),
    )


def assess_simultaneous_requirement(name: str, manifest: dict) -> TestOutcome:
    discovery = _dig(manifest, "snapshot.borrower_discovery.source")
    active = _dig(manifest, "snapshot.borrower_discovery.active_count")
    largest_seized = _dig(manifest, "clearance.largest_borrower_usd")
    largest_debt = _dig(manifest, "clearance.largest_debt_usd")
    top5 = _dig(manifest, "clearance.top5_borrowers_usd")
    combined_debt = _dig(manifest, "books.combined.debt_usd")

    complete = discovery.startswith("complete")
    verdict = PASS if complete else INDETERMINATE
    finding = (
        f"from a {discovery} over {active:,} active accounts, the largest "
        f"single position requires repaying ${largest_debt / 1e6:,.2f}m of "
        f"debt against ${largest_seized / 1e6:,.2f}m of collateral in one "
        f"event; the top five come to ${top5 / 1e6:,.2f}m against a combined "
        f"book of ${combined_debt / 1e6:,.2f}m. The requirement is "
        "concentration-driven rather than spread across the book"
        if complete
        else "the borrower registry is incomplete, so the requirement is not established"
    )

    return TestOutcome(
        number=2,
        question=(
            "how much debt requires repayment simultaneously, or before "
            "capital from earlier liquidations becomes available again"
        ),
        criterion=(
            "the simultaneous repayment requirement is quantified from a "
            "complete borrower registry at the pinned block"
        ),
        verdict=verdict,
        finding=finding,
        evidence=_evidence(
            name,
            manifest,
            "snapshot.borrower_discovery.source",
            "snapshot.borrower_discovery.active_count",
            "clearance.largest_debt_usd",
            "clearance.largest_borrower_usd",
            "clearance.top5_borrowers_usd",
            "books.combined.debt_usd",
        ),
    )


def assess_financing_capacity(name: str, manifest: dict) -> TestOutcome:
    required = _dig(manifest, "clearance.largest_borrower_usd")
    clearable_quiet = _dig(manifest, "clearance.max_clearable_usd_quiet")
    clearable_stressed = _dig(manifest, "clearance.max_clearable_usd_stressed")
    passes_quiet = _dig(manifest, "clearance.passes_quiet")
    passes_stressed = _dig(manifest, "clearance.passes_stressed")
    breakeven = _dig(manifest, "clearance.breakeven_slippage")
    is_lower_bound = _dig(manifest, "clearance.slippage_quiet_is_lower_bound")

    verdict = PASS if (passes_quiet and passes_stressed) else FAIL
    shortfall = required / clearable_quiet if clearable_quiet else float("inf")
    finding = (
        f"instant executable capacity inside the {breakeven:.2%} break-even "
        f"slippage is ${clearable_quiet / 1e6:,.2f}m quiet and "
        f"${clearable_stressed / 1e6:,.2f}m stressed, against a "
        f"${required / 1e6:,.2f}m single-event requirement: short by a "
        f"factor of {shortfall:,.0f} in the quiet case"
    )
    missing: tuple[str, ...] = ()
    if is_lower_bound:
        missing = (
            "aggregator depth beyond the quoted notional, so the measured "
            "slippage is a lower bound and the true shortfall is at least "
            "this large",
        )

    return TestOutcome(
        number=3,
        question="can eligible liquidators finance that repayment when required",
        criterion=(
            "instant executable capacity inside the liquidation bonus is at "
            "least the simultaneous repayment requirement from test 2"
        ),
        verdict=verdict,
        finding=finding,
        evidence=_evidence(
            name,
            manifest,
            "clearance.largest_borrower_usd",
            "clearance.max_clearable_usd_quiet",
            "clearance.max_clearable_usd_stressed",
            "clearance.passes_quiet",
            "clearance.passes_stressed",
            "clearance.breakeven_slippage",
        ),
        missing=missing,
        who_can_supply=("DEX aggregators", "market makers") if missing else (),
    )


def assess_bonus_adequacy(
    name: str, manifest: dict, canonical_loss: float = 0.04
) -> TestOutcome:
    bonus = manifest["liquidation_bonus"]
    key = f"{canonical_loss:.6f}"
    sweep = manifest["canonical_loss_sweep"][key]
    throughput = manifest["redemption_throughput_usd_per_day"]

    rows = {
        regime: {
            "minimum_bonus": entry["minimum_bonus"],
            "days": entry["result"]["time_to_clear_hours"] / 24.0,
            "passes": entry["result"]["economic_clearance_pass"],
            "uses_redemption": entry["result"]["redemption_exit_usd"] > 0.0,
        }
        for regime, entry in sweep.items()
    }
    dex_only = {k: v for k, v in rows.items() if not v["uses_redemption"]}
    with_redemption = {k: v for k, v in rows.items() if v["uses_redemption"]}

    dex_clears = any(v["passes"] for v in dex_only.values())
    redemption_clears = any(v["passes"] for v in with_redemption.values())

    if dex_clears:
        verdict = PASS
        finding = (
            f"the {bonus:.2%} bonus covers the modelled costs on evidenced "
            "exit capacity alone"
        )
        missing: tuple[str, ...] = ()
    elif redemption_clears:
        verdict = INDETERMINATE
        worst_dex = max(v["minimum_bonus"] for v in dex_only.values())
        best = min(v["minimum_bonus"] for v in with_redemption.values())
        finding = (
            f"the {bonus:.2%} bonus is insufficient on evidenced exit "
            f"capacity alone, where the required minimum runs from "
            f"{min(v['minimum_bonus'] for v in dex_only.values()):.2%} to "
            f"{worst_dex:.2%}. It clears only on the route that assumes "
            f"${throughput['quiet'] / 1e6:,.0f}m a day of primary "
            f"redemption, which lowers the requirement to {best:.2%}. That "
            "throughput is a stated assumption, not a measured entitlement, "
            "so the outcome cannot be called a pass"
        )
        missing = (
            "a contractual or evidenced primary redemption entitlement: "
            "daily throughput, cut-off, fee, gating, and suspension terms",
        )
    else:
        verdict = FAIL
        finding = (
            f"the {bonus:.2%} bonus does not cover the modelled costs on any "
            "route considered"
        )
        missing = ()

    return TestOutcome(
        number=4,
        question="does the bonus compensate for settlement and recovery risk",
        criterion=(
            "the liquidation bonus is at least the minimum economic bonus on "
            "a route whose exit capacity is evidenced rather than assumed"
        ),
        verdict=verdict,
        finding=finding,
        evidence=(
            *_evidence(
                name,
                manifest,
                "liquidation_bonus",
                "redemption_throughput_usd_per_day",
            ),
            # The sweep key contains a decimal point, so it is cited
            # directly rather than through a dotted path, and summarised
            # rather than embedded whole.
            Evidence(
                manifest=name,
                field=f"canonical_loss_sweep[{key}]",
                value={
                    regime: {
                        "minimum_bonus": row["minimum_bonus"],
                        "days_to_clear": row["days"],
                        "economic_clearance_pass": row["passes"],
                        "uses_redemption": row["uses_redemption"],
                    }
                    for regime, row in rows.items()
                },
            ),
        ),
        missing=missing,
        who_can_supply=("the redemption counterparty", "the issuer")
        if missing
        else (),
    )


def combine(tests: tuple[TestOutcome, ...]) -> tuple[str, str]:
    """Overall clearance, respecting the ordering of the tests."""

    failed = [t for t in tests if t.verdict == FAIL]
    if failed:
        first = min(failed, key=lambda t: t.number)
        later = [t.number for t in tests if t.number > first.number]
        if later:
            subject = (
                f"Test {later[0]} remains"
                if len(later) == 1
                else f"Tests {', '.join(str(n) for n in later)} remain"
            )
            tail = (
                f" {subject} informative but conditional, and cannot "
                f"establish clearance while test {first.number} fails."
            )
        else:
            tail = ""
        return FAIL, f"test {first.number} fails on its stated criterion.{tail}"

    unresolved = [t for t in tests if t.verdict == INDETERMINATE]
    if unresolved:
        numbers = ", ".join(str(t.number) for t in unresolved)
        subject = (
            f"test {numbers} cannot be resolved"
            if len(unresolved) == 1
            else f"tests {numbers} cannot be resolved"
        )
        return (
            INDETERMINATE,
            f"no test fails, but {subject} on the available evidence, and "
            "missing data is never a pass.",
        )
    return PASS, "every test meets its stated criterion on the cited evidence."


def build_assessment(
    reachability: tuple[str, dict],
    market: tuple[str, dict],
    balance_sheet: tuple[str, dict],
    stress_path: str,
    canonical_loss: float = 0.04,
) -> Assessment:
    tests = (
        assess_oracle_reachability(*reachability),
        assess_simultaneous_requirement(*market),
        assess_financing_capacity(*market),
        assess_bonus_adequacy(*balance_sheet, canonical_loss=canonical_loss),
    )
    overall, reason = combine(tests)

    blocks = {
        _dig(reachability[1], "fixture.block"),
        _dig(market[1], "snapshot.block"),
        _dig(balance_sheet[1], "snapshot.block"),
    }
    return Assessment(
        chain=_dig(market[1], "snapshot.chain"),
        asset=_dig(market[1], "snapshot.asset"),
        block=_dig(market[1], "snapshot.block"),
        block_timestamp=_dig(market[1], "snapshot.timestamp"),
        stress_path=stress_path,
        tests=tests,
        overall=overall,
        overall_reason=reason,
        vintage_consistent=len(blocks) == 1,
    )
