# Case Study: Risk Stewards Cap Reductions on Aave V3 Linea (July 2026)

An independent quantitative reproduction of a live governance decision,
using this repository's data layer and risk engine. The point is not to
second-guess the decision but to test whether an independent pipeline,
built from public keyless sources, reaches the same conclusions from the
same market.

Sections 1 through 4 preserve the initial July 18 reproduction. The July 30
and August 17 refreshes appear in the addendum.

## The Decision

On July 1, 2026, LlamaRisk posted
[Risk Stewards: Supply and Borrow Cap Reductions on Aave V3 / 2026.07.01](https://governance.aave.com/t/risk-stewards-supply-and-borrow-cap-reductions-on-aave-v3-2026-07-01/25269),
reducing caps across Aave V3 Linea. For WETH specifically:

| Parameter | Old | New |
|---|---:|---:|
| Supply cap | 7,950 WETH | 6,250 WETH |
| Borrow cap | 7,150 WETH | 2,370 WETH |

The stated rationale: caps were set with "limited margin above current
outstanding supply", borrow caps were brought "toward current outstanding
borrowing", and on-chain DEX liquidity was identified as the key concern.
The post's headline liquidity observation:

> WETH reaches its liquidation bonus threshold at approximately $41,000 of
> sell size against $8.4M supplied.

## Independent Reproduction

Snapshot: Aave V3 Linea, block 31,426,233 (July 18, 2026), built with
`python -m aave_risk_engine.data.build_snapshot --chain linea --asset WETH`.
All inputs are keyless public sources: Linea JSON-RPC for reserve state and
borrower discovery, KyberSwap routed quotes for depth, Kraken for the
volatility calibration.

### 1. The implemented caps verify on-chain

| Check | On-chain (block 31,426,233) | Governance post |
|---|---:|---:|
| Supply cap | 6,250 WETH | 6,250 WETH |
| Borrow cap | 2,370 WETH | 2,370 WETH |
| Supplied | 4,969.5 WETH ($9.17m) | ~$8.4m at post time |
| Borrowed | 2,368.7 WETH | n/a |

The borrow cap is 99.9% utilized: the Stewards brought it to current
outstanding borrowing almost exactly, and it now binds.

### 2. The depth observation reproduces independently

Routed KyberSwap sell quotes (WETH to USDC on Linea, July 18):

| Sale size | Slippage |
|---:|---:|
| $15,000 | 1.31% |
| $41,000 | 6.98% |
| $100,000 | 38.5% |
| $300,000 | 75.3% |
| $1,000,000 | 92.2% |

Liquidator break-even at a 6% bonus is `0.06 / 1.06 = 5.66%`. Interpolating
the quote curve, the largest sale clearable within the bonus is about
**$32,500**, measured 17 days after the post and via a different
aggregator. LlamaRisk's figure was **~$41,000**. Same order of magnitude,
same conclusion, slightly more conservative on the later date: the
market's instant exit capacity for WETH on Linea is a few tens of
thousands of dollars, against $9m supplied.

The ARFC clearance test formalizes what that means:

```text
largest WETH-collateral borrower sale : $46,400
max clearable within bonus (quiet)    : $32,500  -> FAIL
max clearable (50% depth haircut)     : $16,200  -> FAIL
```

Even the largest single WETH-collateral borrower could not be liquidated
within the bonus in calm conditions. Under the Aave Risk Framework's
liquidation-capacity requirement, this market fails on instant on-chain
depth alone, which supports the reduction direction unambiguously.

### 3. What the borrower book shows

Of 44 sampled accounts with debt (from a ~28-day Borrow-event scan), the
five largest are all leveraged loopers: 100% WETH-denominated debt against
LST collateral, with health factors of 1.00 to 1.23. Positions at HF 1.00
sit exactly at the liquidation boundary; any wstETH/ETH or weETH/ETH
exchange-rate wobble pushes them into a liquidation queue that, per the
depth curve above, stalls almost immediately.

The USD-debt book against WETH collateral is tiny ($96k across 5
accounts; CVaR99 of $760), so the engine's USD-shock lens confirms the
market's risk does not sit in conventional stable-debt borrowing. It sits
in exit liquidity and correlated loops, which is exactly the vector the
Stewards cited.

### 4. Cap headroom in model terms

The supply cap times LTV still permits up to $9.2m of debt against WETH
collateral. A cap sweep on the (scaled) real book shows a $500k CVaR99
budget is breached at roughly $290k of USD-debt exposure, because any
meaningful liquidation queue immediately exceeds the clearable depth. The
cap is therefore not the binding risk control on this market; liquidity
is. Right-sizing caps toward observed usage, as the Stewards did, reduces
the headroom for that gap to grow.

## Divergences and Honest Limitations

- Our $32.5k clearance figure vs their $41k: different quote date (17 days
  apart), different aggregator routing, and possibly different slippage
  conventions. The agreement is in magnitude and conclusion, not the third
  significant digit.
- Borrower discovery scans recent Borrow events (~28 days here), so
  dormant positions are not sampled. The reserve-level supply and borrow
  totals are exact regardless.
- The combined book revalues ETH-denominated debt, but the effective
  single-asset mapping does not separately shock every LST collateral leg.
  It therefore does not fully model each looper's LST/ETH exchange-rate risk.
- Instant routed depth understates total exit capacity where redemption
  queues exist; for WETH itself there is no queue, so the strict reading
  is appropriate here.

## Conclusion

An independent pipeline reproduces the decision's inputs (caps, usage,
depth) from public data, confirms its headline liquidity number within
measurement noise, and reaches the same verdict through a formal test:
Linea WETH liquidation capacity cannot clear even its largest single
borrower within the liquidation bonus, so shrinking cap headroom toward
observed usage was the right risk call.

## Addendum: July 30 And August 17 Refreshes

Two later snapshots show that the independent clearance estimate remained
close to LlamaRisk's reference while the verdict stayed unchanged:

| Quantity | Jul 18 | Jul 30 | Aug 17 |
|---|---:|---:|---:|
| Snapshot block | 31,426,233 | 31,568,531 | 31,741,470 |
| Max clearable within bonus | $32,500 | $41,770 | $42,420 |
| Slippage at a $41k sale | 6.98% | 5.06% | 4.59% |
| Largest borrower sale | $46,400 | $46,440 | $46,560 (still FAIL) |
| WETH supplied | 4,970 | 4,831 | 4,641 |
| Caps (supply / borrow) | 6,250 / 2,370 | unchanged | unchanged |

![Linea WETH empirical depth](../assets/linea_weth_depth.png)

The figure uses the August 17 KyberSwap quote ladder. The black marker is
LlamaRisk's approximately $41,000 reference size, the orange marker is the
$46,560 largest sampled borrower sale, and the horizontal line is liquidator
break-even at 5.66%.

Rebuild it from the committed snapshot with:

```bash
python -m aave_risk_engine.run_market_report --snapshot data/snapshots/aave_v3_linea_weth.json --budget 500000 --figure
```

The July 30 snapshot remains recoverable at commit `5876690`. The committed
release snapshot and figure now use August 17.

Borrower-book rows are not a clean panel across the three vintages because
discovery coverage changed. Reserve totals, caps, and quote ladders are direct
observations and do not depend on that borrower sample.

Our clearance measure moved from below LlamaRisk's approximately $41,000
figure to close to it on two separate refreshes. This is consistent with a
noisy independent estimate of the same underlying quantity. Reserve-level
supply continued to fall, and the clearance verdict is unchanged: the largest
sampled borrower still cannot be liquidated within the bonus on instant
on-chain depth.
