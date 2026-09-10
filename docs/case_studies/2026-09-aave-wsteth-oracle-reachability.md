# Oracle Reachability: Aave V3 Ethereum wstETH

Case date: September 10, 2026. Pinned block 25,946,216, timestamp
1789034015. Verdict on test 1: **PASS**, with two caveats that matter
more than the verdict.

## The question

Every liquidation calculation in this repository assumes something it
never checks: that when collateral moves, the protocol sees the move. A
bounded feed can refuse to publish it. A price without a timestamp
cannot be rejected as stale. Test 1 of the four-test sequence asks
whether the stress can be represented and transmitted at all, before
anyone asks whether the bonus is adequate.

This case answers it for one integration, at one block, from primary
evidence.

## Reproduce

```text
python -m aave_risk_engine.run_oracle_reachability \
    --manifest docs/manifests/ethereum-wsteth-oracle-reachability-25946216.json
```

The analysis is offline. It reads a committed fixture,
`data/oracle/ethereum-wsteth-25946216.json`, built once by the only
networked step:

```text
python -m aave_risk_engine.data.build_oracle_fixture \
    --chain ethereum --asset wstETH --block 25946216 \
    --out data/oracle/ethereum-wsteth-25946216.json
```

Function selectors are derived from their signatures with a stdlib
Keccak-256 in `data/abi.py`, not hardcoded, so a mistyped signature
produces a revert rather than a silent call to a different function.
The builder aborts if any probe goes unanswered, because recording an
endpoint outage as "the contract does not support this" would put an
unverified claim into the evidence.

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
liquidation bonus. That bonus is the same parameter used in the
[liquidator balance sheet](../results.md), so the two analyses describe
the same reserve rather than two different books.

`getPooledEthByShares(uint256)` was identified by searching the adapter
bytecode for the selectors of nine candidate exchange-rate readers. It
was the only one present, and calling it on stETH returns a rate that
reproduces the adapter answer exactly.

## The path is modelled, not assumed

The adapter answer is recomputed from its inputs and compared with the
on-chain value:

```text
base feed answer          2,476.60473800
exchange rate             1.243637988259
cap applied to rate       False
reconstructed answer      3,079.99973407
source latestAnswer       3,079.99973407
AaveOracle getAssetPrice  3,079.99973407
exact match               True (error 0 raw units)
```

This is the difference between a case and a simulator. A configurable
model that reproduces its own assumptions establishes nothing; this one
either reproduces the chain to the raw unit or reports a mismatch and
returns INDETERMINATE. The regression suite asserts the zero error and
asserts that a one-unit perturbation flips the verdict away from PASS.

## Bounds on the path

```text
min at base_feed.aggregator  = 1
max at base_feed.aggregator  = 95780971304118053647396689196894323976171195136475135
deepest floor in price units = 0.00000001
representable fall from here = 100.000000%
```

The Aave-facing source refuses both `minAnswer()` and `maxAnswer()`: it
imposes no bound of its own. The only bounds on the path sit on the
underlying aggregator and are effectively disabled, the maximum being
2^176 - 1.

So the failure mode raised in the HINC and mWIN discussion, where a
bound prevents the stress price from being published at all, does not
apply here. That is the substance of the PASS. It also makes test 1
meaningful: a test that no integration can fail is not a test, and this
one establishes the baseline against which a bounded feed is the
anomaly.

## The growth cap does not constrain a fall

```text
snapshot rate        1.228282498700 at 1772535659
elapsed              16,498,356 s (191.0 days)
max yearly growth    8.80% reported | 8.80% derived | consistent
ceiling on rate now  1.284830205754
current rate         1.243637988259 | headroom 3.3122%
isCapped             False reported | False derived | consistent
```

The cap is a ceiling on the wstETH/stETH exchange rate, limiting how
fast that rate may grow away from a snapshot. It protects against a rate
that rises too quickly. It places no floor under the price and does
nothing to slow a fall.

Two internal consistency checks are run rather than assumed: the
per-second growth constant reproduces the reported yearly percentage,
and the current rate compared with the ceiling reproduces the contract's
own `isCapped()` flag.

## Staleness cannot be enforced on this path

The price source implements `latestAnswer()` and refuses
`latestRoundData()`. It therefore exposes no timestamp at all.

This is not an inference from documentation. If `AaveOracle` called
`latestRoundData()` on this source, `getAssetPrice` would revert. It
does not revert, and it returns the same value as `latestAnswer()`, so
the protocol reads a bare number with no freshness metadata attached.

A timestamp does exist further down: the base feed reports `updated_at`
1789032227, 1,788 seconds before the pinned block. Nothing on the path
reads it.

The consequence is precise and worth stating carefully. Freshness on
this integration is an operational assumption, not a condition enforced
in code. If the underlying feed stopped updating, the adapter would keep
returning a value derived from the last published ETH price, health
factors would keep being computed from it, and no contract on this path
could detect the condition. This is ordinary Aave V3 behaviour rather
than a defect specific to wstETH, which is exactly why it belongs in an
explicit test instead of an unstated assumption.

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

The ACL admin holds `isPoolAdmin` and does not hold the asset listing,
risk, or emergency admin roles. `AaveOracle.setAssetSources` is
restricted to asset listing or pool admins, so the source can be
replaced by governance.

Every control on this path requires an operator transaction. None is
automatic. A response to a stale or broken feed is therefore bounded by
human reaction time, not by a threshold in a contract.

## What this case does not establish

- Behaviour of any other Aave reserve or price source. Each needs its
  own fixture.
- Behaviour at any block other than the pinned one.
- Whether an operator would in fact pause or freeze in time.
- The off-chain publication rules of the underlying feed, including its
  heartbeat and deviation thresholds. A heartbeat setting alone would
  not establish a freshness check on this path, since there is no
  timestamp here to check it against.
- Anything about HINC or mWIN. Those motivated the question. Their
  integrations must be evidenced separately, and behaviour observed here
  must not be transferred to them.

## Why this integration was chosen

The HINC and mWIN integrations motivated the question, but their feed,
adapter, consumer, and reserve controls were not all publicly
identifiable, so they could not satisfy the evidence gate. This reserve
does: every layer is public and readable at a pinned block, without
depending on a proposer or an issuer to supply anything. It also
attaches to work already published on the same reserve.
