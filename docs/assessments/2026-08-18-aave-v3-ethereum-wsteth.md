# Assessment: Aave V3 Ethereum wstETH, block 25,780,402

Worked example of the [four-test format](../assessment-template.md).
Revised September 12, 2026. Overall: **INDETERMINATE**.

## Scope And Decision Criterion

The criterion is coverage of the 99th percentile of first-round repayment
under the published two-day stress distribution. It is not coverage of
every tail scenario or the deterministic largest-position benchmark.
The book contains 855 target-dominant accounts with $964.28m of debt.

The four input manifests share block 25,780,402, timestamp 1787037143.
Test 2 uses the report's scenarios and liquidation sizing. Tests 3 and 4
use its p99 repayment and seizure, not the historical largest-position
warehouse results. The old balance-sheet manifest supplies only the
cost assumptions and regime grid; economics are recomputed.

The relative-value stress is a counterfactual canonical-rate impairment.
An ordinary secondary-market stETH discount need not reach this oracle.
The effective single-asset mapping and the engine's liquidation
approximations still apply.

## Result

| Test | Outcome | Scope |
|---|---|---|
| 1: oracle | PASS | Tested downstream inputs, conditional on delivery |
| 2: requirement | PASS | First round at the single terminal shock |
| 3: financing | INDETERMINATE | Eligible capital is undocumented |
| 3a: atomic only | FAIL | Stressed instant capacity below p99 sale |
| 3b: warehouse only | INDETERMINATE | Full repayment capital unknown |
| 3c: mixed | INDETERMINATE | Residual capital and independence unknown |
| 4: bonus | PASS | Same p99 notional, conditional on the regime grid |

A scoped PASS is not an unconditional statement about market safety.
Upstream publication, actual execution capacity, and financing remain
distinct evidence requirements.

## Test 1: Downstream Oracle Consumption

The baseline reconstruction matches both the source and AaveOracle.
State overrides supply positive ETH/USD answers down to one raw unit
and altered exchange rates to the deployed adapter and oracle. The
observed positive inputs pass through as modelled, including a price
of one raw oracle unit, but not zero.

Forced zero and negative feed answers make the consumer revert. These
are invalid-answer read probes, not rejected transmissions. The
aggregator's signed-report acceptance and stored-round behaviour after
a rejection were not executed and are not inferred from bound getters.

Reverting timestamp getters and a thirty-day-old mock round leave the
downstream price unchanged. This establishes no required dependency on
the probed getters and no freshness rejection in those observations.
It does not exclude caught reads or test the replaced feed's internal
freshness policy. Publication liveness remains outside this PASS.

See the [oracle case](../case_studies/2026-09-aave-wsteth-oracle-reachability.md).

## Test 2: Simultaneous First-Round Requirement

The same snapshot, seed and scenario count reproduce the published
combined-book CVaR99 of $31,824,054.49 before sizing the liquidations.
Debt denomination and collateral-limited liquidation sizing use the
same functions as the existing engine.

| Quantity | p99 | Mean of worst 1% by repayment | Maximum |
|---|---:|---:|---:|
| Debt repayment | $7.15m | $24.14m | $264.62m |
| Collateral seizure | $7.58m | $25.59m | $280.49m |
| Liquidatable positions | 13 | 29.6 | 286 |

The p99 position count is a separate marginal quantile, not necessarily
the count in the p99 repayment scenario. Repayment and seizure have a
common 6% bonus in this run, so the p99 monetary envelope is consistent.

All first-round repayments occur at the single terminal shock. No
intervening recycling is assumed. This does not follow from an exit
horizon: the warehouse model permits earlier cash recovery, including
immediate DEX proceeds. Intrahorizon arrivals, recycling and repeated
liquidations would require an evolving-path capital calculation.

The $256.52m static full-seizure benchmark remains valid for its separate
ARFC clearance question. It is not the p99 requirement, nor an upper
bound on the total sale of all borrowers.

## Test 3: Atomic, Warehouse And Mixed Financing

At the 5.66% theoretical break-even slippage, stressed routed capacity
is $1.37m versus a p99 collateral sale of $7.58m, a factor of 5.5.
The atomic-only screen fails. It assumes accessible flash liquidity
and excludes gas, flash fees and auction payments.

Warehouse-only financing requires $7.15m of gross repayment capital.
Across the aligned grid, full-notional completion takes 0.65 to 6.17
days. The 6.17-day maximum is a conservative commitment bound under
these assumptions, not a minimum holding time or a measured guarantee.

A mixed allocation can repay about $1.29m atomically and leave $5.86m
for independent warehouse capital. It allocates initial DEX capacity
to the atomic tranche, then subtracts that sale from cumulative exit
capacity available to the warehouse. Initial liquidity is not reused.
The full-notional completion horizon is a conservative bound for the
residual allocation; the report records the net-capacity checks.

No such capital is documented in this assessment. A documented line
can establish a conditional PASS if amount, duration and eligibility
cover the selected scope. Combining it with the atomic leg also needs
evidence that the two sources do not overlap. An insufficient identified
line alone cannot establish FAIL. That requires a verified upper bound
on all eligible capital in scope, even allowing a mixed allocation.

A duration shorter than the conservative bound remains unresolved,
rather than demonstrating that a faster feasible schedule is impossible.
Gross repayment excludes additional funding buffers and transaction
costs; those resources also need to be available.

## Test 4: Economics At The Same p99 Notional

Repriced debt: $7,146,279.68. Seized collateral: $7,575,056.47.
Assumptions: 4% canonical recovery loss, 10% annual funding, 10% capital
hurdle, 0.1% hedge entry, 2% annual hedge carry, 1% DEX execution loss.
DEX refill is 6/24 hours; redemption is optionally $25m/day after 24 hours.

| Regime | Completion, days | Minimum bonus | Clears at 6% |
|---|---:|---:|---|
| Quiet DEX | 0.6466 | 5.3454% | yes |
| Stressed DEX | 6.1724 | 5.4993% | yes |
| Quiet + redemption | 1.3030 | 4.3492% | yes |
| Stressed + redemption | 1.3030 | 4.3492% | yes |

All four regimes pass at this notional. Redemption no longer flips the
verdict, unlike the separate $256.52m warehouse benchmark. Its 11.26-day
exit and 6.29%-13.49% DEX bonus requirements must not be imported here.

This is a conditional full-warehouse economics result. It does not
establish measured throughput, economics beyond p99, or profitability
of a mixed route after atomic gas and auction costs. The regime grid,
4% recovery loss and financing rates remain stated assumptions.

### Notional Boundary And Bonus Margin

The bonus stops covering the worst regime at about $22.85m of repayment,
3.2 times the p99 requirement and 95% of the worst-1% mean. The verdict
is scoped to the stated coverage criterion.

| Repayment reference | Debt | Worst minimum bonus | Bonus margin, bp |
|---|---:|---:|---:|
| p99 | $7.15m | 5.4993% | +50.07 |
| Worst-1% mean | $24.14m | 6.0414% | -4.14 |
| Maximum | $264.62m | 14.3156% | -831.56 |

Stressed DEX is the binding regime at all three notionals. The margin
is the 6% bonus minus the minimum economic bonus, in basis points.
The 4.14 bp shortfall at mean tail repayment is sensitive to cost
assumptions as well as notional; it is not a robust universal failure
threshold. Evaluating economics at mean repayment is not averaging
economics over the tail scenarios.

The manifest's test 4 `diagnostics.bonus_notional_sensitivity` records
all regime margins, reference notionals, and a covered/uncovered
boundary bracket narrower than $1. The search scans from p99 to the
observed maximum and bisects the first observed loss of grid coverage
at the fixed 6% bonus. It assumes one transition within that local
bracket, not global monotonicity. Costs and capacities stay fixed;
the numerical tolerance is not economic certainty or a probability.

## Reproduce And Evidence

Run from the repository root after installation:

```bash
python -m aave_risk_engine.run_four_test_assessment
```

Add `--manifest <output.json>` to write a new assessment. The
[committed manifest](../manifests/ethereum-wsteth-four-test-assessment-25780402.json)
records input hashes, recalculated economics and the mixed-capacity
checks. The historical market and largest-position manifests are
preserved. Optional `--financing <evidence.json>` is documented in the
[template](../assessment-template.md).

## Revision Note

The first version confused instant clearance with financing, used the
static largest-position bound for simultaneity, and overstated what
oracle interfaces established. The next revision computed the p99
requirement but still imported the largest-position warehouse economics.

This revision aligns notionals, separates upstream publication from
downstream read probes, permits compatible mixed funding and distinguishes
identified capital from an exhaustive capacity bound. Regression tests
cover these corrections and the bonus coverage boundary above p99,
including cost sensitivity at mean tail repayment. The final result is
PASS, PASS, INDETERMINATE,
PASS, with each PASS restricted to its stated criterion and assumptions.
