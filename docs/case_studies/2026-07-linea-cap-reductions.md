# Case Study: Risk Stewards Cap Reductions on Aave V3 Linea (July 2026)

This case study independently reproduces a governance risk decision with
public keyless data. It does not second-guess the decision; it tests whether a
separate pipeline reaches the same liquidity conclusion.

## The Decision

On July 1, 2026, LlamaRisk posted
[Risk Stewards: Supply and Borrow Cap Reductions on Aave V3 / 2026.07.01](https://governance.aave.com/t/risk-stewards-supply-and-borrow-cap-reductions-on-aave-v3-2026-07-01/25269),
reducing caps across Aave V3 Linea. For WETH:

| Parameter | Old | New |
|---|---:|---:|
| Supply cap | 7,950 WETH | 6,250 WETH |
| Borrow cap | 7,150 WETH | 2,370 WETH |

The post identified on-chain DEX liquidity as the key concern and reported
that WETH reached its liquidation-bonus threshold at approximately $41,000 of
sell size against about $8.4 million supplied.

## Independent Reproduction

The first snapshot was captured at Linea block 31,426,233 on July 18, 2026.
JSON-RPC verified the implemented caps at 6,250 WETH supplied and 2,370 WETH
borrowed. It also measured 4,969.5 WETH supplied and 2,368.7 WETH borrowed, so
the new borrow cap was already 99.9% utilized.

An independent KyberSwap WETH-to-USDC ladder produced:

| Sale size | July 18 slippage |
|---:|---:|
| $15,000 | 1.31% |
| $41,000 | 6.98% |
| $100,000 | 38.5% |
| $300,000 | 75.3% |
| $1,000,000 | 92.2% |

Liquidator break-even at a 6% bonus is `0.06 / 1.06 = 5.66%`.
Interpolating the July 18 curve gives maximum clearable notional of about
$32,500. The estimate was more conservative than LlamaRisk's $41,000, but it
agreed in order of magnitude and supported the same conclusion: instant WETH
exit capacity on Linea was only a few tens of thousands of dollars.

## Quote Convergence

Later quote ladders moved closer to LlamaRisk's reference:

| Quantity | Jul 18 | Jul 30 | Aug 17 | Aug 18 |
|---|---:|---:|---:|---:|
| Snapshot block | 31,426,233 | 31,568,531 | 31,741,470 | 31,749,322 |
| Max clearable within bonus | $32,500 | $41,770 | $42,420 | $43,195 |
| Slippage at a $41k sale | 6.98% | 5.06% | 4.59% | 4.06% |
| WETH supplied | 4,970 | 4,831 | 4,641 | 4,639 |
| Caps, supply / borrow | 6,250 / 2,370 | unchanged | unchanged | unchanged |

Different quote times, routes, and aggregators prevent a uniquely correct
third significant digit. Three later estimates nevertheless cluster around
LlamaRisk's value.

![Linea WETH empirical depth](../assets/linea_weth_depth.png)

The figure uses the August 18 KyberSwap ladder. The black marker is
LlamaRisk's approximately $41,000 reference size, the orange marker is the
$300,470 largest current borrower sale, and the horizontal line is
liquidator break-even at 5.66%.

Rebuild it from the committed snapshot with:

```bash
python -m aave_risk_engine.run_market_report --snapshot data/snapshots/aave_v3_linea_weth.json --budget 500000 --figure
```

## Complete Borrower Discovery

The July 18, July 30, and August 17 snapshot builders used recent `Borrow`
events plus previously known addresses. That rolling method could omit open
positions whose last borrow predated the scan. The August 18 release replaces
it with a persistent registry covering every `Borrow` event from the Linea
Pool proxy deployment at block 12,430,836 through block 31,749,322. It
re-queries all 13,397 historical candidates at the pinned snapshot block and
stores 51 accounts with at least $10,000 of debt.

This correction means old borrower-book rows are not a panel and should not
be read as market growth. Reserve totals, caps, and quote ladders are direct
observations and remain comparable. On the corrected current book, 22
target-dominant accounts enter the USD-debt analysis and the deterministic
clearance test is:

```text
largest WETH-collateral borrower sale : $300,470
max clearable within bonus, quiet     : $43,195 -> FAIL
max clearable with 50% depth haircut  : $21,598 -> FAIL
```

The discovery correction therefore strengthens, rather than reverses, the
same clearance verdict.

## Limitations

- The depth curve is a routed-quote observation, not guaranteed execution.
- WETH has no primary redemption delay, but CEX and OTC liquidity are still
  outside this instant on-chain test.
- The real-book mapping does not separately shock every collateral leg of a
  multi-collateral account.
- The Monte Carlo result is sparse: six positive losses in 20,000 draws do
  not support a precise conditional-severity estimate.

## Conclusion

The independent pipeline verifies the implemented caps, reproduces the
reported liquidation threshold within measurement variation, and reaches the
same decision direction through a formal test. Complete discovery finds that
the current largest WETH-collateral sale is about seven times maximum quiet
clearance capacity. For this market, shrinking cap headroom toward observed
usage is consistent with the measured instant-liquidity constraint.
