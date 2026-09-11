# Oracle Reachability: Aave V3 Ethereum wstETH

Case date: September 10, 2026, revised September 11 after review. Pinned
block 25,946,216, timestamp 1789034015. Verdict on test 1: **PASS**,
with caveats about freshness that matter more than the verdict.

## The question

Every liquidation calculation in this repository assumes something it
never checks: that when collateral moves, the protocol sees the move. A
bound can stop the move from being published, and a path that never
reads a timestamp cannot reject a stale price. Test 1 of the four-test
sequence asks whether the stress can be represented and transmitted at
all, before anyone asks whether the bonus is adequate.

This case answers it for one integration, at one block, against the
behaviour of the deployed contracts.

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

## The four scenarios the roadmap requires

**Accepted stress update.** The base feed was set to half its value, to
one percent of it, and to one raw unit, the aggregator's minimum. Each
time the oracle returned exactly the modelled price, down to 0.00000001.
There is no floor or clamp between today's price and zero on the
executed path, so a fall of any size is representable.

**Rejected update.** Two layers can refuse an update. The aggregator
stores only answers inside its bounds of 1 and 2^176 - 1, so it refuses
nothing above one raw unit of the base feed, which is 0.00000001 USD per
ETH. Its transmission path requires signed reports and was not executed
here. Below the aggregator, a zero or negative value forced through the
feed makes `getAssetPrice` revert rather than return zero. On this
integration, then, no economically meaningful update is refused, and the
only refusal the protocol can see is a failed price read.

**Elapsed time.** With every timestamp getter on the feed made to
revert, the oracle still returned the unchanged price. With a round
thirty days old, it did the same. If anything on the executed path read
a timestamp, the first probe would have reverted; if anything enforced a
threshold of thirty days or less, the second would have.

**Recovery.** A later valid update two percent above the baseline is
reflected at once. The whole path is view-only, so no read can leave
state behind that a later update would have to clear.

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

## Freshness, as four separate properties

These are easy to conflate, so the analysis records them separately:

```text
source exposes a timestamp          False
source bytecode embeds a reader     False
timestamp read on executed path     False
staleness threshold enforced        False
feed round age at the pinned block  1,788 s
```

The first two come from the interface and the bytecode. The last two
are behavioural and settle the question: no timestamp is read anywhere
on the executed path, so no staleness threshold can be enforced there.
Freshness on this integration is an operational assumption, not a
condition enforced in code. This is ordinary Aave V3 behaviour rather
than a defect specific to wstETH, which is exactly why it belongs in an
explicit test instead of an unstated premise.

Because borrowing wstETH is disabled on this reserve, a stale mark would
affect borrowing **against** wstETH as collateral, not borrowing the
asset itself.

## Controls and who holds them

| control | holder | automatic? |
| --- | --- | --- |
| ACL manager | `0xc2aacf6553d20d1e9d78e365aaba8032af9c85b0` | n/a |
| ACL admin | `0x5300a1a15135ea4dc7ad5a167152c01efc9b192a` | n/a |
| replace the price source | pool or asset listing admin | no |
| freeze or pause the reserve | operator transaction | no |

The ACL admin holds `isPoolAdmin` and none of the asset listing, risk, or
emergency admin roles. `AaveOracle.setAssetSources` is restricted to
asset listing or pool admins. Every control on this path requires an
operator transaction; none is automatic.

## What this case does not establish

- Behaviour of any other reserve or price source, or of this reserve at
  another block or after governance replaces the source.
- The aggregator's own transmission path, which was not executed.
- The freshness of the exchange-rate provider's own inputs.
- Whether an operator would in fact pause or freeze in time.
- Anything about HINC or mWIN. Those motivated the question; their
  integrations must be evidenced separately, and behaviour observed here
  must not be transferred to them.

## Why this integration was chosen

The HINC and mWIN integrations motivated the question, but their feed,
adapter, consumer, and reserve controls were not all publicly
identifiable, so they could not satisfy the evidence gate. This reserve
does: every layer is public and executable at a pinned block, without
depending on a proposer or an issuer to supply anything.
