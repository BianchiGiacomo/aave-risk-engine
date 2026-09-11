# Assessment: Aave V3 Ethereum wstETH, block 25,780,402

Worked example of the [four-test assessment format](../assessment-template.md).

| field | value |
| --- | --- |
| chain and market | Ethereum, Aave V3 |
| collateral | wstETH, borrowing disabled, collateral enabled |
| pinned block | 25,780,402, timestamp 1787037143 |
| parameters | LTV 78.5%, liquidation threshold 81%, liquidation bonus 6%, close factor 0.5 |
| stress path | two-day horizon, Student-t returns with jumps, correlated peg and depth stress, reproduced from the market report manifest |
| decision served | is the reserve, as configured at this block, liquidatable under that stress |

All four input manifests are pinned to the same block, so the oracle
configuration, the borrower book, the stress scenarios, and the
liquidator economics describe one system rather than four dates.

## Result

| test | question | outcome |
| --- | --- | --- |
| 1 | can the oracle represent the stress and transmit it | **PASS** |
| 2 | how much debt requires simultaneous repayment | **PASS** |
| 3 | can eligible liquidators finance that repayment | **INDETERMINATE** |
| | 3a instant clearance, atomic liquidators | FAIL within its scope |
| | 3b warehouse financing | INDETERMINATE |
| 4 | does the bonus compensate for settlement and recovery risk | **INDETERMINATE** |

**Overall clearance: INDETERMINATE.** No test fails, but tests 3 and 4
cannot be resolved on the available evidence, and missing data is never a
pass. Instant clearance fails within its own scope, which does not decide
the test it belongs to.

The format neither clears this reserve nor condemns it. It names the two
inputs that would decide it, and who holds them.

## Test 1: oracle reachability, PASS

*Criterion: the path reproduces the recorded source and oracle prices,
and the deployed contracts pass stress updates through without a bound
or floor above the stress level.*

The baseline price is reproduced exactly and matches both the source and
`AaveOracle`. Executed through state overrides at this block, every
accepted stress update down to one raw unit of the base feed passed
through the deployed adapter and oracle as modelled, so a fall of 100%
is representable. A zero or negative base value makes `getAssetPrice`
revert; the aggregator's bounds already refuse such values.

*Missing: an operational guarantee of feed publication.* With every
timestamp getter on the feed reverting, and with a thirty-day-old round,
the oracle returned the unchanged price. No timestamp is read on the
executed path, so freshness is an operational assumption. The feed
operator and Aave governance hold that guarantee.

Full evidence in the
[wstETH oracle reachability case study](../case_studies/2026-09-aave-wsteth-oracle-reachability.md).

## Test 2: simultaneous requirement, PASS

*Criterion: the requirement is computed on the stated stress path, from a
complete borrower registry, with protocol liquidation sizing, and with
capital recycling inside the horizon accounted for.*

The scenario set is rebuilt from the same snapshot, seed, and count as
the published market report, and reproduces its combined-book CVaR99 of
$31,824,054.49 exactly before any liquidation is sized. In each scenario,
every position with a health factor below one is sized with the
protocol's own rules: full repayment below a health factor of 0.95,
otherwise half, capped by seizable collateral, with ETH-denominated debt
moving with the ETH return.

```text
per scenario            p50      p90      p95      p99      max   tail mean
repayment (debt)      $0.00m   $0.03m   $0.05m   $7.15m  $264.62m   $24.14m
seizure (collateral)  $0.00m   $0.03m   $0.05m   $7.58m  $280.49m   $25.59m
positions                  0        2        3       13       286      29.6
```

Repayment is debt the liquidator must finance; seizure is collateral it
receives and must sell or hold. They are reported separately and never
compared with each other.

At the 99% level, 13 positions are liquidatable at once, requiring $7.15m
of repayment for $7.58m of collateral. Over the worst 1% of scenarios the
mean is $24.14m and $25.59m, and the single largest repayment is on
average 42% of the total. The full seizure of the largest position,
$256.52m of collateral, sits at the extreme of the distribution: it is a
conservative bound, not the tail requirement.

Capital does not recycle inside the two-day horizon, because the fastest
modelled exit takes 11.26 days, so every repayment in a scenario is
concurrent.

*Limits: first liquidation round only; the stress is a single horizon
shock, as in the published report.*

## Test 3: financing, INDETERMINATE

*Criterion: either instant clearance covers the simultaneous collateral
sale, or evidenced financing covers the simultaneous repayment for the
exit horizon. FAIL requires both to be shown insufficient.*

The question is whether someone can finance the repayment. Two
populations can answer it, and they have different constraints.

**3a Instant clearance, FAIL within its scope.** Atomic liquidators repay
with flash liquidity and must sell the collateral in the same
transaction. Routed depth inside the 5.66% break-even slippage clears
$2.73m quiet and $1.37m under the stated depth stress, against $7.58m of
collateral to be sold at once at the 99% level: short by a factor of 5.5
stressed and 2.8 quiet.

**3b Warehouse financing, INDETERMINATE.** A liquidator that repays with
its own or borrowed capital would need $7.15m at the 99% level, and
$24.14m on average over the tail, committed at the moment of liquidation
and held for at least 11.26 days. No evidence of committed liquidator
capital is available.

The instant FAIL shows that atomic liquidators alone cannot meet the
requirement. It does not show that nobody can finance the repayment and
hold the collateral while exiting, which is the question the test asks.
That distinction is the reason the
[liquidator balance sheet](../results.md) exists.

*Missing: committed liquidator financing, meaning balance-sheet
capacity, credit lines, or a backstop arrangement of at least the
simultaneous repayment for the exit horizon.* Liquidators, market makers,
or a DAO-arranged backstop hold that evidence.

## Test 4: bonus adequacy, INDETERMINATE

*Criterion: at the stated canonical loss, the bonus is at least the
minimum economic bonus in every regime of the stated grid, which must
include a regime as adverse as the stated depth stress. If the outcome
flips across the grid, the verdict is INDETERMINATE and names the inputs
that flip it.*

At a 4% canonical recovery loss, with 10% annual funding, a separate 10%
capital hurdle, 0.1% hedge entry, 2% annual hedge carry and 1% DEX
execution loss:

```text
regime                    days to clear   minimum bonus   clears at 6%
quiet DEX                        30.11           6.29%    no
stressed DEX                    241.88          13.49%    no
quiet + redemption               11.26           4.66%    yes
stressed + redemption            11.26           4.66%    yes
```

The bonus clears in two of four regimes. The outcome flips between
regimes that differ only in redemption throughput: with the stated $25m
a day it clears, without it it does not.

Every input of this grid is a stated sensitivity rather than a
measurement: DEX refill times of 6 and 24 hours, the stressed depth
haircut, and redemption throughput alike. None of the routes is
evidenced exit capacity. What separates redemption from refill is not
that one is assumed and the other measured, but that within the grid
the refill assumption does not change the outcome while redemption
does. The redemption regimes also clear only while the canonical loss
stays below 5.21%.

*Missing: evidence for redemption throughput.* For wstETH that is
primary redemption through the Lido withdrawal queue, whose throughput
and delay under stress are partly observable on-chain but not measured
here.

## What would change the result

- Evidence of committed liquidator financing of at least $7.15m for 11.26
  days would resolve test 3 in one direction or the other, whatever
  instant depth does.
- Measured withdrawal-queue throughput under stress would resolve test 4.
- Deeper routed liquidity would move sub-test 3a, which would pass on its
  own at about 5.5 times today's depth under the stated stress.

## What this assessment does not establish

- Any other reserve, chain, date, or stress path.
- That the reserve is safe or unsafe. It says that under this stress path
  at this block the stress reaches the protocol, the tail requirement is
  $7.15m of repayment, atomic liquidators alone cannot clear it, and the
  two inputs that would decide the rest are unmeasured.

## Reproduce

```bash
python -m aave_risk_engine.run_simultaneous_requirement --manifest docs/manifests/ethereum-wsteth-simultaneous-requirement-2026-08-18.json
python -m aave_risk_engine.run_four_test_assessment --manifest docs/manifests/ethereum-wsteth-four-test-assessment-25780402.json
```

Every verdict above is derived from a field in one of four pinned
manifests, listed with the assessment output and recorded in
[the assessment manifest](../manifests/ethereum-wsteth-four-test-assessment-25780402.json).

## Revision note

A first version of this assessment, dated September 10, returned an
overall FAIL. Review found that its test 3 decided financing from DEX
depth alone, its test 2 took the requirement from a static
single-position bound rather than the stress path, its test 1 treated
the absence of an exposed timestamp as proof that none is read and
ignored a mismatch against the oracle price, and its test 4 labelled
DEX routes as evidenced while treating redemption as assumed. Each is
corrected above, and each correction has a regression test.
