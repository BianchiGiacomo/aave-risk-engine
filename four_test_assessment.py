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
import copy
import math
from dataclasses import dataclass, asdict, field, replace
from functools import lru_cache
from itertools import combinations
from pathlib import Path

from .liquidator_balance_sheet import (
    LiquidatorAssumptions,
    minimum_liquidation_bonus,
    simulate_liquidator_balance_sheet,
)
from .time_to_exit import (
    ExitAssumptions, dex_capacity_at, redemption_capacity_at,
)

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
    diagnostics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FinancingEvidence:
    """Gross repayment capital documented for eligible liquidators.

    exhaustive means the amount is a verified upper bound on all eligible
    warehouse capital in scope. independent_of_atomic permits combining
    it with flash-funded atomic repayment without double counting.
    """

    committed_usd: float
    holding_days: float
    source: str
    exhaustive: bool = False
    independent_of_atomic: bool = False

    def __post_init__(self) -> None:
        values = (self.committed_usd, self.holding_days)
        if any(not math.isfinite(v) or v < 0 for v in values):
            raise ValueError(
                "financing amounts must be finite and non-negative"
            )
        if not self.source.strip():
            raise ValueError("financing evidence needs a source")


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
    economics: dict


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


@lru_cache(maxsize=32)
def _reprice_grid(debt: float, seized: float, loss: float,
                  assumptions_json: str, regimes_json: str) -> dict:
    """Cache economic sizing by every numerical input."""
    assumptions = replace(
        LiquidatorAssumptions(**json.loads(assumptions_json)),
        canonical_loss=loss,
    )
    rows = {}
    for name, inputs in json.loads(regimes_json).items():
        exit_params = ExitAssumptions(**inputs)
        result = simulate_liquidator_balance_sheet(
            debt, seized, exit_params, assumptions
        )
        minimum = minimum_liquidation_bonus(debt, exit_params, assumptions)
        rows[name] = {
            "minimum_bonus": minimum,
            "result": {
                "time_to_clear_hours": result.time_to_clear_hours,
                "cleared": result.cleared,
                "economic_clearance_pass": result.economic_clearance_pass,
                "economic_profit_usd": result.economic_profit_usd,
                "peak_capital_usd": result.peak_capital_usd,
                "first_cash_recovery_hours": next((
                    c.horizon_hours for c in result.cash_flows
                    if c.cash_recovery_usd > 0
                ), None),
            },
        }
    return rows


def reprice_balance_sheet(sim: dict, sheet: dict, loss: float) -> dict:
    """Reuse the historical assumptions, not the historical results."""
    debt = sim["results"]["repayment_usd"]["p99"]
    seized = sim["results"]["seizure_usd"]["p99"]
    if debt <= 0 or not math.isclose(
        seized, debt * (1 + sheet["liquidation_bonus"]), rel_tol=1e-9
    ):
        raise ValueError("p99 debt and seizure must form one bonus envelope")
    rows = _reprice_grid(
        debt, seized, loss,
        json.dumps(sheet["common_assumptions"], sort_keys=True),
        json.dumps(sheet["regimes"], sort_keys=True),
    )
    return {
        "scope": (
            "p99 first-round requirement; not the static largest position"
        ),
        "debt_repaid_usd": debt,
        "seized_collateral_usd": seized,
        "liquidation_bonus": sheet["liquidation_bonus"],
        "canonical_loss": loss,
        "repayment_reference_usd": {
            key: sim["results"]["repayment_usd"][key]
            for key in ("p99", "tail_mean", "maximum")
        },
        "tail_level": sim["results"]["tail_level"],
        "common_assumptions": {
            **sheet["common_assumptions"], "canonical_loss": loss,
        },
        "regimes": copy.deepcopy(sheet["regimes"]),
        "canonical_loss_sweep": {_sweep_key(loss): copy.deepcopy(rows)},
        "notes": [
            "All exit and cost assumptions remain conditional sensitivities.",
            "Recomputed at the same p99 notional used by tests 2 and 3.",
        ],
    }


def holding_bound_days(sheet: dict, loss: float) -> float | None:
    """Conservative full-notional completion bound across all regimes."""
    rows = sheet["canonical_loss_sweep"][_sweep_key(loss)].values()
    days = [
        r["result"]["time_to_clear_hours"] / 24
        if r["result"]["cleared"] else None for r in rows
    ]
    return max(days) if days and all(d is not None for d in days) else None


def assess_oracle_reachability(name: str, manifest: dict) -> TestOutcome:
    results = manifest["results"]
    verdict = results["verdict"]
    fresh = results.get("freshness") or {}
    rec = results.get("reconstruction") or {}

    if verdict == PASS:
        finding = (
            "the baseline price is reproduced exactly and matches both the "
            "source and AaveOracle. The tested positive inputs down to one "
            "raw unit passed through the deployed adapter and consumer as "
            "modelled, including a fall to nearly zero. This is conditional "
            "on upstream delivery, not a test of update publication"
        )
    else:
        finding = "; ".join(results.get("reasons", [])) or "not established"

    missing: tuple[str, ...] = ()
    who: tuple[str, ...] = ()
    if fresh.get("enforced") is False:
        finding += (
            ". Reverting timestamp getters and a thirty-day-old round left "
            "the downstream price unchanged. No freshness rejection was "
            "observed in these probes; upstream checks were replaced"
        )
        missing = (
            "upstream publication, liveness, and rejected-round behaviour, "
            "which were not executed",
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
            "the deployed adapter and consumer pass the tested positive "
            "inputs through, conditional on upstream delivery"
        ),
        verdict=verdict,
        finding=finding,
        evidence=(
            *_evidence(
                name,
                manifest,
                "results.verdict",
                "results.max_verified_fall",
                "results.freshness.timestamp_getters_required_on_probed_path",
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
            "behaviour verified at this block through eth_call "
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
                else "the scenario set does not reproduce the published run"
            ),
            evidence=_evidence(
                sim_name, sim, "stress_path.reproduces_published_scenarios"
            ),
        )

    recycling = (
        "All first-round repayments occur at the single horizon shock; "
        "no intervening capital recycling is assumed. This is not inferred "
        "from a time-to-exit calculation"
    )

    finding = (
        f"on the published stress path, reproduced exactly, "
        f"the p99 requirement is {_m(repay['p99'])} of debt repayment "
        f"and {_m(seize['p99'])} of collateral. The p99 position count is "
        f"{count['p99']:.0f}, a separate marginal quantile. Over the worst "
        f"{1 - level:.0%} of scenarios the mean is {_m(repay['tail_mean'])} "
        f"of repayment and {_m(seize['tail_mean'])} of collateral. In those "
        f"tail scenarios the single largest repayment is on average "
        f"{results['largest_share_of_tail_repayment']:.1%} of the total. "
        f"The static largest-position benchmark is {_m(static_bound)} of "
        f"collateral, versus {_m(seize['maximum'])} of total collateral in "
        f"the worst scenario. These are distinct criteria. {recycling}"
    )

    return TestOutcome(
        number=2,
        question=(
            "how much debt requires repayment simultaneously, or before "
            "capital from earlier liquidations becomes available again"
        ),
        criterion=(
            "the requirement is computed on the stated stress path, from a "
            "complete borrower registry, with modelled liquidation sizing, "
            "with the first-round simultaneity assumption stated"
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
            "p99 is the coverage criterion, not a bound on the worst 1%",
            "relative-value stress is a counterfactual canonical-rate "
            "impairment, not an ordinary secondary-market depeg trigger",
        ),
    )


def assess_financing_capacity(
    sim_name: str, sim: dict, market_name: str, market: dict,
    balance_sheet: dict, canonical_loss: float,
    financing: FinancingEvidence | None = None,
) -> TestOutcome:
    results = sim["results"]
    seized = results["seizure_usd"]["p99"]
    debt = results["repayment_usd"]["p99"]
    capacity = _dig(market, "clearance.max_clearable_usd_stressed")
    atomic_sale = min(capacity, seized)
    atomic_debt = debt * atomic_sale / seized
    residual = max(0.0, debt - atomic_debt)
    hold = holding_bound_days(balance_sheet, canonical_loss)
    mixed_capacity = {}
    if hold is not None:
        for name, inputs in balance_sheet["regimes"].items():
            params = ExitAssumptions(**inputs)
            available = (
                dex_capacity_at(params, 24 * hold)
                + redemption_capacity_at(params, 24 * hold)
            )
            mixed_capacity[name] = {
                "warehouse_initial_capacity_usd": max(
                    0.0, dex_capacity_at(params, 0) - atomic_sale
                ),
                "warehouse_capacity_at_bound_usd": max(
                    0.0, available - atomic_sale
                ),
                "warehouse_seizure_usd": seized - atomic_sale,
            }
    schedule_ok = bool(mixed_capacity) and all(
        r["warehouse_capacity_at_bound_usd"] + 1e-6
        >= r["warehouse_seizure_usd"] for r in mixed_capacity.values()
    )
    duration_ok = (
        financing is not None and hold is not None
        and financing.holding_days >= hold
    )
    warehouse = mixed = INDETERMINATE
    if financing is not None:
        if financing.committed_usd >= debt and duration_ok:
            warehouse = PASS
        elif financing.exhaustive and financing.committed_usd < debt:
            warehouse = FAIL
        if (financing.committed_usd >= residual and duration_ok
                and financing.independent_of_atomic and schedule_ok):
            mixed = PASS
        elif (financing.exhaustive
              and financing.committed_usd + atomic_debt < debt):
            mixed = FAIL
    instant = PASS if residual <= 1e-8 else FAIL
    verdict = (
        PASS if PASS in (instant, warehouse, mixed)
        else FAIL if mixed == FAIL else INDETERMINATE
    )
    hold_text = (
        f"{hold:.2f} days across the aligned regime grid"
        if hold is not None else "an unresolved exit horizon"
    )
    finding = (
        f"p99 repayment {_m(debt)}; atomic sale capacity {_m(atomic_sale)} "
        f"supports {_m(atomic_debt)} of repayment, leaving {_m(residual)} "
        "for warehouse capital in a mixed allocation. The conservative "
        f"holding bound is {hold_text}; it is not a minimum holding time "
        "and does not forbid earlier cash recovery. "
        + ("No committed warehouse financing is documented."
           if financing is None else
           f"Documented: {_m(financing.committed_usd)} for "
           f"{financing.holding_days:g} days ({financing.source}). "
           "Insufficient identified capital is not a market-wide upper "
           "bound unless its evidence explicitly establishes that scope.")
    )
    evidence = (
        *_evidence(market_name, market,
                   "clearance.max_clearable_usd_stressed"),
        *_evidence(sim_name, sim, "results.repayment_usd.p99",
                   "results.seizure_usd.p99"),
        Evidence("derived_economics", "canonical_loss_sweep",
                 balance_sheet["canonical_loss_sweep"]),
        Evidence("derived_mixed_capacity", "net_of_atomic_sale",
                 mixed_capacity),
    )
    if financing is not None:
        evidence += (
            Evidence(financing.source, "financing", asdict(financing)),
        )
    return TestOutcome(
        number=3,
        question="can eligible liquidators finance that repayment",
        criterion=(
            "atomic, warehouse, or a compatible mixed allocation covers "
            "p99 repayment; FAIL requires an evidenced aggregate capacity "
            "upper bound below the requirement"
        ),
        verdict=verdict, finding=finding, evidence=evidence,
        missing=(() if verdict == PASS else (
            "eligible warehouse capital and duration, with explicit scope "
            "and no double counting against the atomic leg",
        )),
        who_can_supply=("liquidators and backstop providers",),
        limits=(
            "gross debt repayment only; fees and funding buffers need "
            "additional resources, whose costs are tested separately",
            "atomic capacity assumes accessible flash liquidity and excludes "
            "gas, flash fees and auction payments",
            "mixed allocation gives initial DEX depth to the atomic leg; "
            "warehouse exits use remaining cumulative capacity, not the "
            "same initial depth twice",
            "the full-notional completion bound is conservative for the "
            "residual; shorter financing duration is unresolved, not FAIL",
            "PASS is conditional on the exit-capacity grid; it does not "
            "prove those capacities are available",
        ),
        sub_outcomes=(
            SubOutcome(
                "3a instant clearance", "atomic-only benchmark",
                "stressed routed capacity covers the p99 sale", instant,
                f"{_m(capacity)} versus {_m(seized)}",
            ),
            SubOutcome(
                "3b warehouse financing", "warehouse-only benchmark",
                "documented capital covers gross repayment for the bound",
                warehouse, f"full repayment {_m(debt)}; bound {hold_text}",
            ),
            SubOutcome(
                "3c mixed financing", "non-overlapping allocation",
                "atomic repayment plus independent warehouse capital "
                "covers the requirement",
                mixed,
                f"atomic {_m(atomic_debt)}; residual {_m(residual)}",
            ),
        ),
    )


def _regime_rows(balance_sheet: dict, canonical_loss: float) -> dict:
    bonus = balance_sheet["liquidation_bonus"]
    sweep = balance_sheet["canonical_loss_sweep"][_sweep_key(canonical_loss)]
    return {
        regime: {
            "inputs": balance_sheet["regimes"][regime],
            "minimum_bonus": entry["minimum_bonus"],
            "days_to_clear": (
                entry["result"]["time_to_clear_hours"] / 24.0
                if entry["result"]["cleared"] else None
            ),
            "passes": (entry["minimum_bonus"] is not None
                       and entry["minimum_bonus"] <= bonus),
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
            sorted(k for k in a["inputs"]
                   if a["inputs"][k] != b["inputs"].get(k))
        )
        pairs.append(diff)
    if not pairs:
        return ()
    narrowest = min(len(diff) for diff in pairs)
    return tuple(sorted({
        k for diff in pairs if len(diff) == narrowest for k in diff
    }))


@lru_cache(maxsize=32)
def _bonus_notional_sensitivity(
    bonus: float, loss: float, references_json: str,
    assumptions_json: str, regimes_json: str,
) -> dict:
    """Locate a covered-to-uncovered bracket, not a global capacity bound.

    Hold costs and capacities fixed and scale seizure with repayment.
    Scan above p99, then bisect the first observed loss of grid coverage.
    The bracket assumes one transition locally; unobserved crossings and
    execution capacity beyond this deterministic grid are not established.
    """
    references = json.loads(references_json)
    if any(not math.isfinite(v) or v <= 0 for v in references.values()):
        raise ValueError("repayment reference amounts must be positive")
    start, stop = references["p99"], references["maximum"]
    if not start <= references["tail_mean"] <= stop:
        raise ValueError("repayment references must be ordered")
    regimes = json.loads(regimes_json)
    params = {name: ExitAssumptions(**p) for name, p in regimes.items()}
    assumptions = replace(
        LiquidatorAssumptions(**json.loads(assumptions_json)),
        canonical_loss=loss,
    )

    def coverage(debt):
        results = {
            name: simulate_liquidator_balance_sheet(
                debt, debt * (1 + bonus), p, assumptions
            ) for name, p in params.items()
        }
        profits = {k: r.economic_profit_usd for k, r in results.items()}
        failures = [k for k, v in profits.items() if v is not None and v < 0]
        if failures:
            return False, failures
        if any(v is None for v in profits.values()):
            return None, []
        return True, []

    samples = {}
    for label, debt in references.items():
        rows = _reprice_grid(
            debt, debt * (1 + bonus), loss,
            assumptions_json, regimes_json,
        )
        resolved = all(r["minimum_bonus"] is not None for r in rows.values())
        worst = max(rows, key=lambda k: rows[k]["minimum_bonus"] or 0)
        minimum = rows[worst]["minimum_bonus"] if resolved else None
        samples[label] = {
            "repayment_usd": debt,
            "seized_collateral_usd": debt * (1 + bonus),
            "worst_regime": worst if resolved else None,
            "worst_minimum_bonus": minimum,
            "bonus_margin_bps": (
                (bonus - minimum) * 1e4 if resolved else None
            ),
            "all_regimes_cover": coverage(debt)[0],
            "regimes": {
                name: {
                    "minimum_bonus": row["minimum_bonus"],
                    "bonus_margin_bps": (
                        (bonus - row["minimum_bonus"]) * 1e4
                        if row["minimum_bonus"] is not None else None
                    ),
                    "economic_profit_usd": row["result"][
                        "economic_profit_usd"
                    ],
                } for name, row in rows.items()
            },
        }

    boundary = {
        "status": "not_found_in_range",
        "repayment_usd": None,
        "covered_lower_usd": None,
        "uncovered_upper_usd": None,
        "binding_regimes": [],
        "multiple_of_p99": None,
        "fraction_of_tail_mean": None,
        "search_range_usd": [start, stop],
        "scan_intervals": 32,
        "tolerance_usd": 1.0,
        "method": "log scan then local bisection of economic profit at bonus",
    }
    initial = samples["p99"]["all_regimes_cover"]
    if initial is not True:
        boundary["status"] = (
            "not_covered_at_p99" if initial is False else "unresolved"
        )
    else:
        low = start
        for index in range(1, 33):
            high = start * (stop / start) ** (index / 32)
            covers, failures = coverage(high)
            if covers is None:
                boundary["status"] = "unresolved"
                break
            if covers:
                low = high
                continue
            for _ in range(60):
                if high - low <= boundary["tolerance_usd"]:
                    break
                mid = (low + high) / 2
                covers, failed_mid = coverage(mid)
                if covers is None:
                    break
                if covers:
                    low = mid
                else:
                    high, failures = mid, failed_mid
            if covers is None:
                boundary["status"] = "unresolved"
                break
            estimate = (low + high) / 2
            boundary.update({
                "status": "bracketed",
                "repayment_usd": estimate,
                "covered_lower_usd": low,
                "uncovered_upper_usd": high,
                "binding_regimes": failures,
                "multiple_of_p99": estimate / start,
                "fraction_of_tail_mean": estimate / references["tail_mean"],
            })
            break
    return {
        "liquidation_bonus": bonus,
        "canonical_loss": loss,
        "samples": samples,
        "coverage_boundary": boundary,
        "margin_definition": "(stated bonus - minimum bonus) * 10000",
        "scope": (
            "fixed cost and exit grid; local upper transition above p99, "
            "not a global monotonicity proof or a tail coverage probability; "
            "economics at mean repayment is not mean tail economics"
        ),
    }


def _notional_limit(sensitivity: dict) -> str:
    boundary = sensitivity["coverage_boundary"]
    if boundary["status"] != "bracketed":
        return (
            "The verdict is scoped to the stated coverage criterion. "
            "No notional boundary is established: " + boundary["status"]
        )
    return (
        "The bonus stops covering the worst regime at about "
        f"{_m(boundary['repayment_usd'])} of repayment, "
        f"{boundary['multiple_of_p99']:.1f} times the p99 requirement and "
        f"{boundary['fraction_of_tail_mean']:.0%} of the worst-"
        f"{1 - sensitivity['tail_level']:.0%} mean. "
        "The verdict is scoped to the stated coverage criterion."
    )


def assess_bonus_adequacy(
    name: str,
    balance_sheet: dict,
    market: dict,
    canonical_loss: float = 0.04,
) -> TestOutcome:
    bonus = balance_sheet["liquidation_bonus"]
    rows = _regime_rows(balance_sheet, canonical_loss)
    capacities = [
        row["inputs"]["instant_dex_capacity_usd"] for row in rows.values()
    ]
    if not capacities:
        raise ValueError("economic assessment requires a regime grid")
    sensitivity = copy.deepcopy(_bonus_notional_sensitivity(
        bonus, canonical_loss,
        json.dumps(balance_sheet["repayment_reference_usd"], sort_keys=True),
        json.dumps(balance_sheet["common_assumptions"], sort_keys=True),
        json.dumps(balance_sheet["regimes"], sort_keys=True),
    ))
    sensitivity["tail_level"] = balance_sheet["tail_level"]
    haircut = _dig(market, "clearance.depth_haircut_stressed")
    adverse_needed = max(capacities) * (1.0 - haircut)
    has_adverse = any(c <= adverse_needed * (1.0 + 1e-9) for c in capacities)
    passing = [r for r, row in rows.items() if row["passes"]]
    flips = flipping_inputs(rows)
    inconsistent = [
        r for r, row in rows.items() if row["passes"] != row["flag"]
    ]

    table = "; ".join(
        (f"{regime} {row['minimum_bonus']:.2%} in "
         f"{row['days_to_clear']:.2f} days"
         if row["minimum_bonus"] is not None
         and row["days_to_clear"] is not None else f"{regime}: unresolved")
        for regime, row in rows.items()
    )
    assumptions = (
        "Every input of the grid is a stated sensitivity rather than a "
        "measurement, including DEX refill times, the stressed depth haircut, "
        "and redemption throughput, as the balance-sheet manifest notes"
    )

    if any(r["minimum_bonus"] is None for r in rows.values()):
        verdict = INDETERMINATE
        finding = "at least one economic route could not be resolved"
    elif inconsistent:
        verdict = INDETERMINATE
        finding = (
            "the minimum bonus and the manifest's own clearance flag disagree "
            f"in {', '.join(inconsistent)}"
        )
    elif not has_adverse:
        verdict = INDETERMINATE
        finding = (
            "the grid contains no regime as adverse as the stated depth "
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
            "grid identifies the narrowest observed input differences"
        )
        breakeven = balance_sheet.get("break_even_canonical_loss", {})
        by_level: dict[float, list[str]] = {}
        for regime in passing:
            if breakeven.get(regime) is not None:
                key = round(breakeven[regime], 6)
                by_level.setdefault(key, []).append(regime)
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
            "under stress are not measured here",
        )
        who = ("Lido withdrawal queue data", "the redemption counterparty")

    return TestOutcome(
        number=4,
        question="does the bonus compensate for settlement and recovery risk",
        criterion=(
            "at the stated p99 repayment and canonical loss, the bonus is "
            "at least the minimum "
            "economic bonus in every regime of the stated grid, which must "
            "include a regime as adverse as the stated depth stress; if the "
            "outcome flips across the grid, the verdict is INDETERMINATE and "
            "names the inputs that flip it"
        ),
        verdict=verdict,
        finding=finding,
        diagnostics={"bonus_notional_sensitivity": sensitivity},
        evidence=(
            *_evidence(
                "derived_economics", balance_sheet,
                "repayment_reference_usd", "tail_level",
            ),
            *_evidence(
                name, balance_sheet, "liquidation_bonus", "regimes",
                "common_assumptions"
            ),
            Evidence(
                manifest="derived_economics",
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
        limits=(
            _notional_limit(sensitivity),
            "bonus margins are conditional on cost assumptions as well as "
            "notional; they are not uncertainty bands or probability bounds",
            "conditional on the stated grid and canonical loss; not proof "
            "that modelled exit capacity is available",
            "prices a full warehouse exit, not mixed-route profitability "
            "after atomic gas and auction costs",
        ),
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
        reason = f"test {first.number} fails on its stated criterion.{tail}"
        return FAIL, reason

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
            f" Sub-test {joined}, which does not decide its parent test."
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
    scopes = [
        (reachability[1]["fixture"]["chain"],
         reachability[1]["fixture"]["block"],
         reachability[1]["fixture"]["asset"]["symbol"]),
        *((p[1]["snapshot"]["chain"], p[1]["snapshot"]["block"],
           p[1]["snapshot"]["asset"])
          for p in (market, simultaneity, balance_sheet)),
    ]
    if len(set(scopes)) != 1:
        raise ValueError("inputs disagree on chain, asset or block")
    sheet = reprice_balance_sheet(
        simultaneity[1], balance_sheet[1], canonical_loss
    )
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
    # A funding route and an economic route must be compatible. The current
    # cost engine prices full warehousing, not atomic MEV or mixed execution.
    if (tests[2].verdict == PASS
            and tests[2].sub_outcomes[1].verdict != PASS
            and tests[3].verdict == PASS):
        economic = tests[3]
        tests = (*tests[:3], replace(
            economic,
            verdict=INDETERMINATE,
            finding=(
                economic.finding + ". Financing is supported only through "
                "an atomic or mixed allocation; its route-specific costs "
                "have not been established by the full-warehouse calculation"
            ),
            missing=(
                "economics for the funded atomic or mixed allocation, "
                "including flash fees, gas and auction payments",
            ),
            sub_outcomes=(SubOutcome(
                "4a full warehouse economics", "conditional warehouse grid",
                economic.criterion, PASS, economic.finding,
            ),),
        ))
    overall, reason = combine(tests)

    blocks = {
        _dig(reachability[1], "fixture.block"),
        _dig(market[1], "snapshot.block"),
        _dig(simultaneity[1], "snapshot.block"),
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
        economics=sheet,
    )
