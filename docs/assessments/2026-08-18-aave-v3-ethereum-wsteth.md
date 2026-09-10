# Assessment: Aave V3 Ethereum wstETH, block 25,780,402

Worked example of the [four-test assessment format](../assessment-template.md).

| field | value |
| --- | --- |
| chain and market | Ethereum, Aave V3 |
| collateral | wstETH, borrowing disabled, collateral enabled |
| pinned block | 25,780,402, timestamp 1787037143 |
| parameters | LTV 78.5%, liquidation threshold 81%, liquidation bonus 6%, close factor 0.5 |
| stress path | two-day horizon, Student-t returns with jumps, correlated peg and depth stress, as pinned in the market report manifest |
| decision served | is the reserve, as configured at this block, liquidatable under that stress |

All three input manifests are pinned to the same block, so the oracle
configuration, the borrower book, and the liquidator economics describe
one system rather than three dates.

## Result

| test | question | outcome |
| --- | --- | --- |
| 1 | can the oracle represent the stress and transmit it | **PASS** |
| 2 | how much debt requires simultaneous repayment | **PASS** |
| 3 | can eligible liquidators finance that repayment | **FAIL** |
| 4 | does the bonus compensate for settlement and recovery risk | **INDETERMINATE** |

**Overall clearance: FAIL.** Test 3 fails on its stated criterion. Test 4
remains informative but conditional, and cannot establish clearance while
test 3 fails.

## Test 1: oracle reachability, PASS

*Criterion: the price path reproduces the chain exactly, and no bound on
it prevents the stress mark from being published.*

The price path is a capped adapter over an ETH/USD feed and the stETH
exchange rate. Reconstructing the adapter answer from those inputs
reproduces the on-chain value with an error of zero raw units, so the
path is modelled rather than assumed. No bound stops a fall of any size:
the Aave-facing source refuses `minAnswer()` and `maxAnswer()`, and the
only bounds on the path sit on the underlying aggregator at 1 and
2^176 - 1.

*Missing: an off-chain publication guarantee for the underlying feed.*
The layer the protocol reads exposes no timestamp, so no staleness
threshold can be enforced against this price in code. Freshness is an
operational assumption. The feed operator and Aave governance are the
parties who could supply the guarantee.

Full evidence in the
[wstETH oracle reachability case study](../case_studies/2026-09-aave-wsteth-oracle-reachability.md).

## Test 2: simultaneous requirement, PASS

*Criterion: the requirement is quantified from a complete borrower
registry at the pinned block.*

From a complete Borrow-event registry over 9,526 active accounts, the
largest single position requires repaying $242.00m of debt against
$256.52m of collateral in one event. The top five positions come to
$506.80m against a combined book of $964.28m.

The requirement is concentration-driven rather than spread across the
book. That matters for test 3: the binding constraint is one position,
not the aggregate.

## Test 3: financing and execution capacity, FAIL

*Criterion: instant executable capacity inside the liquidation bonus is
at least the requirement from test 2.*

Break-even slippage at a 6% bonus is 5.66%. Inside that, routed
aggregator depth clears $2.73m in quiet conditions and $1.37m stressed,
against a $256.52m single-event requirement. The quiet case is short by
a factor of 94.

*Missing: aggregator depth beyond the quoted notional.* The measured
slippage is a lower bound, so the true shortfall is at least this large,
never smaller. DEX aggregators and market makers hold that data.

This is the test that decides the assessment. It is also the one that
would be invisible if the four tests were run in any other order, or if
bonus adequacy were treated as the whole question.

## Test 4: bonus adequacy, INDETERMINATE

*Criterion: the bonus is at least the minimum economic bonus on a route
whose exit capacity is evidenced rather than assumed.*

At a 4% canonical recovery loss, with 10% annual funding, a separate 10%
capital hurdle, 0.1% hedge entry, 2% annual hedge carry and 1% DEX
execution loss:

```text
route                     days to clear   minimum bonus   clears at 6%
quiet DEX                        30.11           6.29%    no
stressed DEX                    241.88          13.49%    no
quiet + redemption               11.26           4.66%    yes
stressed + redemption            11.26           4.66%    yes
```

The 6% bonus is insufficient on evidenced exit capacity alone. It clears
only on routes that assume $25m a day of primary redemption, which cuts
the required minimum to 4.66%.

That throughput is a stated sensitivity, not a measured entitlement. The
balance sheet manifest says so directly: *future redemption capacity is
an assumption, not a reserved slot.* An assumption that turns a failing
test into a passing one, where the assumption itself is unevidenced,
produces INDETERMINATE.

*Missing: a contractual or evidenced primary redemption entitlement,
with daily throughput, cut-off, fee, gating, and suspension terms.* The
redemption counterparty and the issuer hold those terms.

## What would change the result

- Evidenced redemption throughput would resolve test 4 in one direction
  or the other, but would not by itself change the overall FAIL, because
  test 3 measures instant capacity at the moment of liquidation.
- Deeper routed liquidity, a higher bonus, or a lower concentration in
  the largest position would each move test 3. The engine reports the
  sensitivity of clearance to each.
- A per-position close factor that splits the largest position across
  several events would reduce the single-event requirement in test 2.
  V3 close factors already do this in part, and the assessment takes the
  full seizure as the conservative bound.

## What this assessment does not establish

- Any other reserve, chain, or date.
- Any stress path other than the pinned one.
- That the reserve is unsafe. It says that under this stress path, at
  this block, instant liquidation capacity does not cover the largest
  position, and that the bonus cannot be judged without redemption
  terms. Whether that is acceptable is a governance decision, not a
  measurement.

## Reproduce

```bash
python -m aave_risk_engine.run_four_test_assessment --manifest docs/manifests/ethereum-wsteth-four-test-assessment-25780402.json
```

Every verdict above is derived from a field in one of three pinned
manifests, listed with the assessment output and recorded in
[the assessment manifest](../manifests/ethereum-wsteth-four-test-assessment-25780402.json).
