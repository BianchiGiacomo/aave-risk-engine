# Oracle Reachability: Aave V3 Ethereum wstETH

Case date: September 10, 2026, revised September 11 after review. Pinned
block 25,946,216, timestamp 1789034015. Verdict on test 1: **PASS**,
for the tested downstream inputs, conditional on upstream delivery.

## The question

The liquidation calculations assume that the protocol sees the modelled
collateral move. A bound can prevent publication, while a downstream
path may consume an answer without requiring freshness evidence.
This case starts checking that assumption. Test 1 of the four-test
sequence asks whether the stress can be represented and transmitted at
all, before anyone asks whether the bonus is adequate.

This case tests downstream price consumption for one integration at two
pinned blocks. It does not execute the upstream publication lifecycle.

## Reproduce

```text
python -m aave_risk_engine.run_oracle_reachability \
    --manifest docs/manifests/ethereum-wsteth-oracle-reachability-25946216.json
```

The analysis is offline against a committed fixture,
`data/oracle/ethereum-wsteth-25946216.json`, built once by the only
networked step:

```text
python -m aave_risk_engine.data.build_oracle_fixture \
    --chain ethereum --asset wstETH --block 25946216 \
    --out data/oracle/ethereum-wsteth-25946216.json
```

Function selectors are derived from their signatures with a stdlib
Keccak-256 in `data/abi.py`. The builder aborts if any probe goes
unanswered, because recording an endpoint outage as "the contract does
not support this" would put an unverified claim into the evidence.

## The path the protocol actually reads

| layer | address | identity |
| --- | --- | --- |
| AaveOracle | `0x54586be62e3c3580375ae3723c145253060ca0c2` | `getAssetPrice` returns 3,079.99973407 |
| price source | `0xe1d97bf61901b075e9626c8a2340a7de385861ef` | `Capped wstETH / stETH(ETH) / USD`, 8 decimals |
| base feed | `0x5424384b256154046e9667ddfaaa5e550145215e` | `ETH / USD`, version 6 |
| aggregator | `0x7c7fdfca295a787ded12bb5c1a49a8d2cc20e3f8` | `DualAggregator 1.0.0` |
| ratio provider | `0xae7ab96520de3a18e5e111b5eaab095312d7fe84` | stETH, read via `getPooledEthByShares(uint256)` |

The fallback oracle is the zero address. The reserve is active, not
frozen, not paused, with LTV 78.5%, liquidation threshold 81%, and a 6%
liquidation bonus, the same parameter used in the liquidator balance
sheet.

`getPooledEthByShares(uint256)` was identified by searching the adapter
bytecode for the selectors of nine candidate exchange-rate readers. It
was the only one present.

## How behaviour was verified

Reading interfaces is not enough. A revert on `minAnswer()` shows the
adapter does not expose a bound; it does not show the adapter imposes
none internally. Refusing `latestRoundData()` shows the adapter does not
expose a timestamp; it does not show the adapter never reads one.

So the fixture also records the contracts' behaviour. For each scenario,
the builder runs one `eth_call` at the pinned block with the code at one
feed address replaced by a small mock serving a stated input, then asks
the real `AaveOracle` for the price. The deployed adapter and oracle
bytecode execute unchanged against that input. Before each observation,
the mock is called directly under the same override on the same
endpoint, and the probe aborts if the override was not honoured. Nothing
is deployed and no state persists.

The builder records observations only. The offline analysis computes
what the path model predicts for each input and compares:

```text
scenario               status    observed          expected          match
baseline               ok           3,079.99973407    3,079.99973407 True
stress_50              ok           1,539.99986703    1,539.99986703 True
stress_99              ok              30.79999734       30.79999734 True
stress_floor           ok               0.00000001        0.00000001 True
zero_answer            reverted                  -                 - -
negative_answer        reverted                  -                 - -
timestamps_unreadable  ok           3,079.99973407    3,079.99973407 True
stale_30_days          ok           3,079.99973407    3,079.99973407 True
recovery               ok           3,141.59972875    3,141.59972875 True
rate_above_cap         ok           3,182.01657509    3,182.01657509 True
rate_drop_20           ok           2,463.99978726    2,463.99978726 True
```

Every observation with a prediction matches to the raw unit, here and at
block 25,780,402 used by the worked assessment. The baseline also
matches both the source's own answer and the price `AaveOracle`
reports: a match against the source alone would not show that the
protocol reads the modelled path, and the analysis now treats a mismatch
against either as INDETERMINATE.

## Behavioural Coverage And Its Boundary

**Positive stress inputs.** The mock base feed supplies half the baseline,
one percent of it, and one raw unit. The deployed adapter and consumer
return the modelled prices, down to 0.00000001. The largest observed fall
is nearly 100%, not a zero price. These finite probes establish the
observed cases; they are not a proof about every possible input.

**Invalid-answer reads, not rejected updates.** Forced zero and negative
feed answers make `getAssetPrice` revert. The real aggregator is bypassed
by the override. Its exposed bounds are recorded as interface evidence;
neither its signed-report acceptance nor storage after rejection was
executed. No conclusion that it refuses only non-positive updates follows.

A rejected transmission and a consumer reading an invalid answer are
different events. To close the original rejected-update requirement,
the former would need its own acceptance/storage evidence. This case
deliberately leaves that item unverified rather than substituting the
invalid-answer probe for it.

**Timestamp dependency.** Reverting mock timestamp getters and a
thirty-day-old mock round both leave the downstream price unchanged.
The tested call therefore does not require those getters to succeed,
and no freshness rejection is observed in those cases. This does not
exclude a caught timestamp read, another timestamp source, or checks
inside the feed whose code was replaced.

**Later valid input.** A separate call with a valid answer two percent
above baseline returns the modelled result. It establishes successful
consumption of that input. The consumer reads do not persist state;
this is not a sequential test of rejected and recovered feed storage.

## The growth cap constrains the rate, not the price

```text
snapshot rate        1.228282498700 at 1772535659
max yearly growth    8.80% reported | 8.80% derived | consistent
isCapped             False reported | False derived | consistent
```

An exchange rate ten percent above the growth-cap ceiling was clamped to
the ceiling, and a rate twenty percent below the current one passed
through unchanged. The cap therefore limits how fast the wstETH/stETH
rate may rise; it places no floor under the price.

## Freshness Evidence

The fixture records source-interface exposure, embedded selector
constants, dependency on the probed timestamp getters, and rejection
observed with an old mock round. These are separate properties.

At both blocks the source exposes no `latestRoundData()`, no timestamp
reader from the probed set appears in its bytecode, and the downstream
price is unchanged by the timestamp probes. The manifest records
`timestamp_getters_required_on_probed_path: false` and
`enforced: false`, where the latter means no freshness rejection was
observed in these probes, not that every upstream policy was inspected.

An old positive answer can therefore be consumed in the tested
downstream conditions. Actual publication liveness, the feed's own
internal checks and the exchange-rate provider's input freshness are
not established. Reserve borrowing and liquidation controls are not
exercised by `getAssetPrice` either.

## Controls and who holds them

| control | holder | automatic? |
| --- | --- | --- |
| ACL manager | `0xc2aacf6553d20d1e9d78e365aaba8032af9c85b0` | n/a |
| ACL admin | `0x5300a1a15135ea4dc7ad5a167152c01efc9b192a` | n/a |
| replace the price source | pool or asset listing admin | not tested |
| freeze or pause the reserve | authorized transaction | not tested |

The ACL admin holds `isPoolAdmin` and none of the asset listing, risk, or
emergency admin roles. `AaveOracle.setAssetSources` is restricted to
asset listing or pool admins. The fixture records these role and state
reads; it does not enumerate all role holders or test automated execution
of reserve controls. A
transaction requirement alone does not imply human reaction time.

## What this case does not establish

- Behaviour of any other reserve or price source, or of this reserve at
  another block or after governance replaces the source.
- The aggregator's transmission path and stored round after rejection.
- Borrowing and liquidation permission under every reserve control.
- The freshness of the exchange-rate provider's own inputs.
- Whether an operator would in fact pause or freeze in time.
- Anything about HINC or mWIN. Those motivated the question; their
  integrations must be evidenced separately, and behaviour observed here
  must not be transferred to them.

## Why this integration was chosen

The HINC and mWIN integrations motivated the question, but their feed,
adapter, consumer, and reserve controls were not all publicly
identifiable, so they could not satisfy the evidence gate. This reserve
supports a reproducible downstream case without private issuer data.
The upstream lifecycle remains a documented limit of this deliverable.
