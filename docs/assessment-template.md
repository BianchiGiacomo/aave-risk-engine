# Four-Test Assessment Template

A format for deciding whether a lending market can be liquidated under a
stated stress, and for saying clearly when it cannot be decided.

The product of a risk assessment is not a model. It is a defensible
decision, or an explicit statement that the decision cannot yet be made
and what would settle it.

## The four tests, in order

1. Can the oracle represent the stress and transmit it to the protocol?
2. How much debt requires repayment simultaneously, or before capital
   from earlier liquidations becomes available again?
3. Can eligible liquidators finance that repayment when required?
4. Does the bonus compensate for settlement and recovery risk?

The order is not presentational. Each test presupposes the ones before
it. Bonus adequacy is meaningless if the stress mark never reaches the
protocol, and financing capacity cannot be judged without knowing the
requirement it has to meet.

## Scope, stated before any test

- Asset, market, and chain.
- A pinned block, with its timestamp.
- The stress path: horizon, return model, and correlated stresses.
- The decision the assessment serves.

Every input must come from the same vintage. An assessment that mixes a
book from one date with an oracle configuration from another is not
describing a single system. The renderer checks this and reports
`one vintage: False` when the inputs disagree.

## For each test, record

- **Question**: what is being decided.
- **Criterion**: the condition that would make this a PASS, stated
  before the evidence is examined.
- **Inputs**: units, timing conventions, and what counts as acceptable
  evidence.
- **Finding**: the measured result.
- **Outcome**: PASS, FAIL, or INDETERMINATE, with reasons.
- **Evidence**: the manifest and the field each claim rests on.
- **Missing evidence**: what is absent, who can supply it, and what it
  would change.

## The three outcomes

**PASS** means the stated criterion is met under the documented scenario
and assumptions.

**FAIL** means it is violated.

**INDETERMINATE** means the available evidence cannot support either
conclusion. This is a result, not a gap in the work. It names the
specific input that is missing and the party who holds it, which is the
form in which a risk assessment is most useful to the people who can act
on it.

**Missing data is never a PASS.** An assumption that makes a test pass,
where the assumption itself is not evidenced, produces INDETERMINATE and
says which assumption it was.

## Rules that keep the verdicts honest

**Treat assumptions symmetrically.** Do not call one route "evidenced"
because its inputs look familiar and another "assumed" because they do
not. Label every input measured or assumed. When a verdict depends on a
grid of assumed inputs, it is PASS only if it holds in every cell, FAIL
only if it holds in none, and otherwise INDETERMINATE, naming the inputs
whose change alone flips the outcome. The grid must include a cell at
least as adverse as the stated stress, so that no pass rests on benign
conditions only.

**Keep a narrower check from deciding a broader question.** A test may
contain sub-outcomes with their own scopes. Instant clearance by atomic
liquidators can fail while the financing question the test asks remains
open, because a liquidator with its own capital can repay and hold the
collateral while it exits. A sub-outcome's FAIL is reported, but it
decides the test only if the test's criterion says so.

**Compute the requirement on the stress path.** A requirement described
next to a stress path is not a requirement computed on it. Test 2 must
size liquidations scenario by scenario, show that the scenario set is
the one it claims to be, report debt to be repaid and collateral to be
sold as separate quantities, and account for whether capital can be
released and reused inside the horizon. A static bound, such as the full
seizure of the largest position, may be reported, but as a bound.

**Verify behaviour, not interfaces.** For test 1, an interface that does
not expose a bound or a timestamp says nothing about whether one is
applied internally. Execute the deployed contracts against stated inputs,
and treat exposing a timestamp, reading one, and enforcing a threshold
as separate properties. A reconstructed price must match what the
protocol actually reads, not only an intermediate layer.

## Combining the tests

- Any FAIL makes overall clearance FAIL.
- With no FAIL, any INDETERMINATE makes overall clearance INDETERMINATE.
- PASS overall requires every test to meet its criterion on cited
  evidence.

Later tests remain informative when an earlier one is unresolved. A
bonus sensitivity is still worth computing when financing capacity has
already failed, because it tells you how far the parameter is from
adequate. But it stays conditional and cannot establish clearance on its
own, and the assessment must say so rather than let a later PASS read as
reassurance.

## Reproducing an assessment

Each verdict is derived from a committed manifest field rather than
written by hand, so a reader can follow any claim back to its evidence:

```bash
python -m aave_risk_engine.run_four_test_assessment \
    --reachability docs/manifests/<oracle reachability>.json \
    --market docs/manifests/<market report>.json \
    --simultaneity docs/manifests/<simultaneous requirement>.json \
    --balance-sheet docs/manifests/<liquidator balance sheet>.json \
    --manifest docs/manifests/<assessment>.json
```

The rules live in `four_test_assessment.py` and are covered by
`tests/test_four_test_assessment.py`. Among other things it asserts that
a failing earlier test dominates a later unresolved one, that a failing
sub-outcome does not decide its test, that a bonus which clears only in
benign regimes is not a pass, that provenance alone does not pass test
2, and that every verdict cites at least one field.

## Limits of the format

The tests are necessary conditions, not a sufficient one. Passing all
four does not make a market safe; it means four specific failure modes
were checked against evidence at one block under one stress path.

The format also does not decide parameters. It reports whether a stated
configuration meets stated criteria, and what is missing when it cannot
be judged. Choosing the criterion, and choosing the stress path, remain
judgment calls that the assessment must expose rather than bury.

## Worked example

[Aave V3 Ethereum wstETH at block 25,780,402](assessments/2026-08-18-aave-v3-ethereum-wsteth.md),
which returns PASS, PASS, INDETERMINATE, INDETERMINATE and an overall
INDETERMINATE. Instant clearance fails within its own scope; financing
and bonus adequacy each wait on one named input.
