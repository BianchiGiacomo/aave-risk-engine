"""Assemble a four-test assessment for one reserve from pinned manifests.

The four tests run in order, and the order matters:

  1. can the oracle represent the stress and transmit it to the protocol;
  2. how much debt requires repayment simultaneously, or before capital
     from earlier liquidations becomes available again;
  3. can eligible liquidators finance that repayment when required;
  4. does the bonus compensate for settlement and recovery risk.

Every verdict here is derived from a committed manifest field rather than
written by hand, and carries the fields it rests on. An outcome is PASS
only when the stated criterion is met on the evidence, FAIL when it is
violated, and INDETERMINATE when the available evidence cannot support
either. Missing data is never a PASS.

A test may carry sub-outcomes with narrower scopes. A sub-outcome can
fail within its scope without deciding the test: instant clearance can
fail while the financing question the test actually asks stays open.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import combinations
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
class SubOutcome:
    """A narrower check inside a test, with its own scope."""

    label: str
    scope: str
    criterion: str
    verdict: str
    finding: str


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
    limits: tuple[str, ...] = ()
    sub_outcomes: tuple[SubOutcome, ...] = ()


@dataclass(frozen=True)
class FinancingEvidence:
    """Evidenced liquidator financing, when someone can supply it."""

    committed_usd: float
    holding_days: float
    source: str


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


def _m(value: float) -> str:
    return f"${value / 1e6:,.2f}m"


def _sweep_key(canonical_loss: float) -> str:
    return f"{canonical_loss:.6f}"


def fastest_exit_days(balance_sheet: dict, canonical_loss: float) -> float | None:
    """Shortest modelled time to clear across the balance-sheet regimes."""

    sweep = balance_sheet["canonical_loss_sweep"][_sweep_key(canonical_loss)]
    days = [
        entry["result"]["time_to_clear_hours"] / 24.0
        for entry in sweep.values()
        if entry["result"]["cleared"]
    ]
    return min(days) if days else None


def assess_oracle_reachability(name: str, manifest: dict) -> TestOutcome:
    results = manifest["results"]
    verdict = results["verdict"]
    fresh = results.get("freshness") or {}
    fall = results.get("max_verified_fall")
    rec = results.get("reconstruction") or {}

    if verdict == PASS:
        finding = (
            "the baseline price is reproduced exactly and matches both the "
            "source and AaveOracle, and every accepted stress update down to "
            "one raw unit of the base feed passed through the deployed "
            f"contracts as modelled, so a fall of {fall:.2%} is verified as "
            "representable"
        )
    else:
        finding = "; ".join(results.get("reasons", [])) or "not established"

    missing: tuple[str, ...] = ()
    who: tuple[str, ...] = ()
    if fresh.get("enforced") is False:
        finding += (
            ". No timestamp is read anywhere on the executed path, verified "
            "by making every timestamp getter revert, so freshness is an "
            "operational assumption rather than an enforced condition"
        )
        missing = (
            "an operational guarantee of feed publication, since no on-chain "
            "freshness check exists on this path",
        )
        who = ("the feed operator", "Aave governance")

    return TestOutcome(
        number=1,
        question=(
            "can the oracle represent the stress and transmit it to the "
            "protocol"
        ),
        criterion=(
            "the path reproduces the recorded source and oracle prices, and "
            "the deployed contracts pass stress updates through without a "
            "bound or floor above the stress level"
        ),
        verdict=verdict,
        finding=finding,
        evidence=(
            *_evidence(
                name,
                manifest,
                "results.verdict",
                "results.max_verified_fall",
                "results.freshness.timestamp_read_on_executed_path",
                "results.freshness.enforced",
            ),
            Evidence(
                name,
                "results.reconstruction.matches_source|matches_oracle",
                [rec.get("matches_source"), rec.get("matches_oracle")],
            ),
        ),
        missing=missing,
        who_can_supply=who,
        limits=(
            "behaviour verified for this source at this block, through eth_call "
            "state overrides; the aggregator's own transmission path was not "
            "executed",
        ),
    )


def assess_simultaneous_requirement(
    sim_name: str,
    sim: dict,
    market_name: str,
    market: dict,
    balance_sheet: dict,
    canonical_loss: float,
) -> TestOutcome:
    discovery = _dig(market, "snapshot.borrower_discovery.source")
    reproduces = _dig(sim, "stress_path.reproduces_published_scenarios")
    horizon = _dig(sim, "stress_path.horizon_days")
    results = sim["results"]
    repay = results["repayment_usd"]
    seize = results["seizure_usd"]
    count = results["liquidatable_positions"]
    level = results["tail_level"]
    static_bound = results["static_bound_largest_full_seizure_usd"]
    exit_days = fastest_exit_days(balance_sheet, canonical_loss)

    complete = discovery.startswith("complete")
    if not complete or not reproduces:
        return TestOutcome(
            number=2,
            question=(
                "how much debt requires repayment simultaneously, or before "
                "capital from earlier liquidations becomes available again"
            ),
            criterion=(
                "the requirement is computed on the stated stress path from a "
                "complete borrower registry"
            ),
            verdict=INDETERMINATE,
            finding=(
                "the borrower registry is incomplete"
                if not complete
                else "the scenario set does not reproduce the published stress path"
            ),
            evidence=_evidence(
                sim_name, sim, "stress_path.reproduces_published_scenarios"
            ),
        )

    if exit_days is not None and exit_days > horizon:
        recycling = (
            f"Capital does not recycle inside the {horizon:g}-day horizon: the "
            f"fastest modelled exit takes {exit_days:.2f} days, so every "
            "repayment in a scenario is concurrent"
        )
    else:
        recycling = (
            "The fastest modelled exit is shorter than the horizon, so capital "
            "could recycle and the concurrent requirement is overstated"
        )

    finding = (
        f"on the published stress path, reproduced exactly, "
        f"{count['p99']:.0f} positions are liquidatable at once at the "
        f"{level:.0%} level, requiring {_m(repay['p99'])} of debt repayment "
        f"in exchange for {_m(seize['p99'])} of collateral; over the worst "
        f"{1 - level:.0%} of scenarios the mean is {_m(repay['tail_mean'])} "
        f"of repayment and {_m(seize['tail_mean'])} of collateral. In those "
        f"tail scenarios the single largest repayment is on average "
        f"{results['largest_share_of_tail_repayment']:.1%} of the total. "
        f"The full seizure of the largest position, {_m(static_bound)} of "
        f"collateral, sits at the extreme of the distribution, where the "
        f"worst scenario seizes {_m(seize['maximum'])}: it is a conservative "
        f"bound, not the tail requirement. {recycling}"
    )

    return TestOutcome(
        number=2,
        question=(
            "how much debt requires repayment simultaneously, or before "
            "capital from earlier liquidations becomes available again"
        ),
        criterion=(
            "the requirement is computed on the stated stress path, from a "
            "complete borrower registry, with protocol liquidation sizing, and "
            "with capital recycling inside the horizon accounted for"
        ),
        verdict=PASS,
        finding=finding,
        evidence=(
            *_evidence(
                market_name, market, "snapshot.borrower_discovery.source"
            ),
            *_evidence(
                sim_name,
                sim,
                "stress_path.reproduces_published_scenarios",
                "stress_path.horizon_days",
                "results.repayment_usd",
                "results.seizure_usd",
                "results.liquidatable_positions",
                "results.static_bound_largest_full_seizure_usd",
            ),
        ),
        limits=(
            "first liquidation round only; a second round inside the horizon "
            "is not counted",
            "the stress is a single horizon shock, as in the published report",
        ),
    )


def assess_financing_capacity(
    sim_name: str,
    sim: dict,
    market_name: str,
    market: dict,
    balance_sheet: dict,
    canonical_loss: float,
    financing: FinancingEvidence | None = None,
) -> TestOutcome:
    results = sim["results"]
    level = results["tail_level"]
    seize_req = results["seizure_usd"]["p99"]
    repay_req = results["repayment_usd"]["p99"]
    tail_repay = results["repayment_usd"]["tail_mean"]
    quiet = _dig(market, "clearance.max_clearable_usd_quiet")
    stressed = _dig(market, "clearance.max_clearable_usd_stressed")
    breakeven = _dig(market, "clearance.breakeven_slippage")
    max_quoted = _dig(market, "clearance.max_quoted_usd")
    exit_days = fastest_exit_days(balance_sheet, canonical_loss)

    instant_ok = stressed >= seize_req
    instant_finding = (
        f"routed depth inside the {breakeven:.2%} break-even slippage clears "
        f"{_m(quiet)} quiet and {_m(stressed)} under the stated depth stress, "
        f"against {_m(seize_req)} of collateral to be sold at once at the "
        f"{level:.0%} level"
        + (
            f": short by a factor of {seize_req / stressed:.1f} stressed and "
            f"{seize_req / quiet:.1f} quiet"
            if not instant_ok
            else ""
        )
    )
    if seize_req > max_quoted:
        instant_finding += (
            "; the requirement exceeds the largest quoted notional, so the "
            "shortfall is a lower bound"
        )
    instant = SubOutcome(
        label="3a instant clearance",
        scope=(
            "atomic liquidators, who repay with flash liquidity and must sell "
            "the collateral in the same transaction"
        ),
        criterion=(
            "instant executable capacity inside the bonus, under the stated "
            "depth stress, is at least the simultaneous collateral sale"
        ),
        verdict=PASS if instant_ok else FAIL,
        finding=instant_finding,
    )

    hold = f" for at least {exit_days:.2f} days" if exit_days is not None else ""
    if financing is None:
        warehouse = SubOutcome(
            label="3b warehouse financing",
            scope=(
                "liquidators who repay with their own or borrowed capital and "
                "exit over days"
            ),
            criterion=(
                "evidenced committed financing is at least the simultaneous "
                "repayment, available for the modelled exit horizon"
            ),
            verdict=INDETERMINATE,
            finding=(
                f"a warehouse liquidator would need {_m(repay_req)} at the "
                f"{level:.0%} level, and {_m(tail_repay)} on average over the "
                f"tail, committed at the moment of liquidation and held{hold}. "
                "No evidence of committed liquidator capital is available, so "
                "whether that financing exists cannot be determined"
            ),
        )
    else:
        enough = (
            financing.committed_usd >= repay_req
            and (exit_days is None or financing.holding_days >= exit_days)
        )
        warehouse = SubOutcome(
            label="3b warehouse financing",
            scope=(
                "liquidators who repay with their own or borrowed capital and "
                "exit over days"
            ),
            criterion=(
                "evidenced committed financing is at least the simultaneous "
                "repayment, available for the modelled exit horizon"
            ),
            verdict=PASS if enough else FAIL,
            finding=(
                f"{_m(financing.committed_usd)} committed for "
                f"{financing.holding_days:g} days ({financing.source}) against "
                f"{_m(repay_req)} required{hold}"
            ),
        )

    if instant.verdict == PASS or warehouse.verdict == PASS:
        verdict = PASS
    elif warehouse.verdict == FAIL:
        verdict = FAIL
    else:
        verdict = INDETERMINATE

    if verdict == INDETERMINATE:
        finding = (
            "instant clearance fails within its own scope, so atomic "
            "liquidators alone cannot meet the requirement. That does not "
            "show that nobody can finance the repayment and hold the "
            "collateral while exiting, which is the question this test asks; "
            "on the available evidence that question stays open"
        )
    elif verdict == PASS:
        finding = "; ".join(
            f"{s.label}: {s.verdict}" for s in (instant, warehouse)
        )
    else:
        finding = (
            "neither atomic clearance nor evidenced warehouse financing covers "
            "the simultaneous requirement"
        )

    return TestOutcome(
        number=3,
        question="can eligible liquidators finance that repayment when required",
        criterion=(
            "either instant clearance covers the simultaneous collateral sale, "
            "or evidenced financing covers the simultaneous repayment for the "
            "exit horizon; FAIL requires both to be shown insufficient"
        ),
        verdict=verdict,
        finding=finding,
        evidence=(
            *_evidence(
                market_name,
                market,
                "clearance.max_clearable_usd_quiet",
                "clearance.max_clearable_usd_stressed",
                "clearance.breakeven_slippage",
            ),
            *_evidence(
                sim_name, sim, "results.seizure_usd.p99", "results.repayment_usd.p99"
            ),
        ),
        missing=(
            ()
            if financing is not None
            else (
                "committed liquidator financing: balance-sheet capacity, "
                "credit lines, or a backstop arrangement of at least the "
                "simultaneous repayment for the exit horizon",
            )
        ),
        who_can_supply=(
            ()
            if financing is not None
            else ("liquidators", "market makers", "a DAO-arranged backstop")
        ),
        sub_outcomes=(instant, warehouse),
    )


def _regime_rows(balance_sheet: dict, canonical_loss: float) -> dict:
    bonus = balance_sheet["liquidation_bonus"]
    sweep = balance_sheet["canonical_loss_sweep"][_sweep_key(canonical_loss)]
    return {
        regime: {
            "inputs": balance_sheet["regimes"][regime],
            "minimum_bonus": entry["minimum_bonus"],
            "days_to_clear": entry["result"]["time_to_clear_hours"] / 24.0,
            "passes": entry["minimum_bonus"] <= bonus,
            "flag": entry["result"]["economic_clearance_pass"],
        }
        for regime, entry in sweep.items()
    }


def flipping_inputs(rows: dict) -> tuple[str, ...]:
    """Inputs whose change alone flips the outcome between two regimes.

    Among pairs of regimes with different outcomes, only the pairs that
    differ in the fewest inputs are kept, so a flip is attributed to the
    narrowest difference the grid can isolate.
    """

    pairs = []
    for (_, a), (_, b) in combinations(rows.items(), 2):
        if a["passes"] == b["passes"]:
            continue
        diff = tuple(
            sorted(k for k in a["inputs"] if a["inputs"][k] != b["inputs"].get(k))
        )
        pairs.append(diff)
    if not pairs:
        return ()
    narrowest = min(len(diff) for diff in pairs)
    return tuple(sorted({k for diff in pairs if len(diff) == narrowest for k in diff}))


def assess_bonus_adequacy(
    name: str,
    balance_sheet: dict,
    market: dict,
    canonical_loss: float = 0.04,
) -> TestOutcome:
    bonus = balance_sheet["liquidation_bonus"]
    rows = _regime_rows(balance_sheet, canonical_loss)
    capacities = [row["inputs"]["instant_dex_capacity_usd"] for row in rows.values()]
    haircut = _dig(market, "clearance.depth_haircut_stressed")
    adverse_needed = max(capacities) * (1.0 - haircut)
    has_adverse = any(c <= adverse_needed * (1.0 + 1e-9) for c in capacities)
    passing = [r for r, row in rows.items() if row["passes"]]
    flips = flipping_inputs(rows)
    inconsistent = [r for r, row in rows.items() if row["passes"] != row["flag"]]

    table = "; ".join(
        f"{regime} {row['minimum_bonus']:.2%} in {row['days_to_clear']:.2f} days"
        for regime, row in rows.items()
    )
    assumptions = (
        "Every input of the grid is a stated sensitivity rather than a "
        "measurement, including DEX refill times, the stressed depth haircut, "
        "and redemption throughput, as the balance-sheet manifest notes"
    )

    if inconsistent:
        verdict = INDETERMINATE
        finding = (
            "the minimum bonus and the manifest's own clearance flag disagree "
            f"in {', '.join(inconsistent)}"
        )
    elif not has_adverse:
        verdict = INDETERMINATE
        finding = (
            "the regime grid contains no regime as adverse as the stated depth "
            "stress, so a pass would rest on benign conditions only"
        )
    elif len(passing) == len(rows):
        verdict = PASS
        finding = (
            f"the {bonus:.2%} bonus clears in every regime of the grid, "
            f"including the most adverse: {table}. {assumptions}"
        )
    elif not passing:
        verdict = FAIL
        finding = (
            f"the {bonus:.2%} bonus clears in no regime of the grid: {table}. "
            f"{assumptions}"
        )
    else:
        verdict = INDETERMINATE
        finding = (
            f"at a {canonical_loss:.0%} canonical recovery loss the "
            f"{bonus:.2%} bonus clears in {len(passing)} of {len(rows)} "
            f"regimes: {table}. The outcome flips between regimes that differ "
            f"only in {', '.join(flips)}. {assumptions}; within the grid the "
            "refill assumption does not change the outcome, while the "
            "flipping input does"
        )
        breakeven = balance_sheet.get("break_even_canonical_loss", {})
        by_level: dict[float, list[str]] = {}
        for regime in passing:
            if breakeven.get(regime) is not None:
                by_level.setdefault(round(breakeven[regime], 6), []).append(regime)
        limits = [
            f"{' and '.join(regimes)} clear{'s' if len(regimes) == 1 else ''} "
            f"only while the canonical loss stays below {level:.2%}"
            for level, regimes in sorted(by_level.items())
        ]
        if limits:
            finding += ". " + "; ".join(limits)

    missing: tuple[str, ...] = ()
    who: tuple[str, ...] = ()
    if verdict == INDETERMINATE and flips:
        missing = (
            "evidence for the flipping input "
            f"({', '.join(flips)}); for wstETH that is primary redemption "
            "through the Lido withdrawal queue, whose throughput and delay "
            "under stress are partly observable on-chain but not measured here",
        )
        who = ("Lido withdrawal queue data", "the redemption counterparty")

    return TestOutcome(
        number=4,
        question="does the bonus compensate for settlement and recovery risk",
        criterion=(
            "at the stated canonical loss, the bonus is at least the minimum "
            "economic bonus in every regime of the stated grid, which must "
            "include a regime as adverse as the stated depth stress; if the "
            "outcome flips across the grid, the verdict is INDETERMINATE and "
            "names the inputs that flip it"
        ),
        verdict=verdict,
        finding=finding,
        evidence=(
            *_evidence(
                name, balance_sheet, "liquidation_bonus", "regimes", "notes"
            ),
            Evidence(
                manifest=name,
                field=f"canonical_loss_sweep[{_sweep_key(canonical_loss)}]",
                value={
                    regime: {
                        "minimum_bonus": row["minimum_bonus"],
                        "days_to_clear": row["days_to_clear"],
                        "passes": row["passes"],
                    }
                    for regime, row in rows.items()
                },
            ),
        ),
        missing=missing,
        who_can_supply=who,
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
        numbers = [str(t.number) for t in unresolved]
        subject = (
            f"test {numbers[0]} cannot be resolved"
            if len(numbers) == 1
            else f"tests {' and '.join(numbers)} cannot be resolved"
        )
        scoped = [
            f"{s.label} fails within its own scope"
            for t in tests
            for s in t.sub_outcomes
            if s.verdict == FAIL
        ]
        joined = "; ".join(scoped)
        note = (
            f" Sub-test {joined}, which does not decide the test it belongs to."
            if scoped
            else ""
        )
        return (
            INDETERMINATE,
            f"no test fails, but {subject} on the available evidence, and "
            f"missing data is never a pass.{note}",
        )
    return PASS, "every test meets its stated criterion on the cited evidence."


def build_assessment(
    reachability: tuple[str, dict],
    market: tuple[str, dict],
    simultaneity: tuple[str, dict],
    balance_sheet: tuple[str, dict],
    stress_path: str,
    canonical_loss: float = 0.04,
    financing: FinancingEvidence | None = None,
) -> Assessment:
    sheet = balance_sheet[1]
    tests = (
        assess_oracle_reachability(*reachability),
        assess_simultaneous_requirement(
            *simultaneity, *market, sheet, canonical_loss
        ),
        assess_financing_capacity(
            *simultaneity, *market, sheet, canonical_loss, financing
        ),
        assess_bonus_adequacy(
            balance_sheet[0], sheet, market[1], canonical_loss=canonical_loss
        ),
    )
    overall, reason = combine(tests)

    blocks = {
        _dig(reachability[1], "fixture.block"),
        _dig(market[1], "snapshot.block"),
        _dig(simultaneity[1], "snapshot.block"),
        _dig(sheet, "snapshot.block"),
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
