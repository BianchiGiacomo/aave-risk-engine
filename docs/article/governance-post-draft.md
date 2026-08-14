# Independent Liquidation-Capacity Stress Tests For Aave

## Scope And Disclosure

> **Independent research prototype.** This work is unaffiliated with Aave
> Labs, the Aave DAO, or LlamaRisk. It is not a parameter recommendation,
> automated risk agent, or substitute for Risk Steward judgment. The purpose
> is to offer an independent, open-source quantitative lens on Risk Steward
> decisions and V4 liquidation design, with assumptions and reproducible
> code open to challenge.

Repository: [BianchiGiacomo/aave-risk-engine](https://github.com/BianchiGiacomo/aave-risk-engine)

Methodology: [METHODOLOGY.md](https://github.com/BianchiGiacomo/aave-risk-engine/blob/master/METHODOLOGY.md)

This note asks a narrow question: when stressed collateral reaches the
liquidation queue, is immediate market depth sufficient to clear the relevant
borrowers within the liquidation bonus?

## Method

The pipeline uses keyless public sources only: Aave state and account data
from JSON-RPC, routed sell quotes from Paraswap or KyberSwap, ETH history from
Kraken, and LST ratio history from DefiLlama. Recent Borrow events discover
accounts, which are then queried on-chain and mapped into real borrower books.
The engine simulates collateral returns, peg moves, and depth evaporation,
places underwater accounts into a liquidation queue, and measures bad debt
with 99% CVaR. Liquidators participate while execution slippage remains below
`bonus / (1 + bonus)`. Ordered clearing processes positions sequentially by
bonus and seize size, with each cleared tranche consuming depth. Committed
snapshots make the reports deterministic and offline.

The repository includes:

- deterministic Aave market snapshots and report CLIs;
- V3 and V4 liquidation mechanics with ordered queue clearing;
- historical episode replay on today's book;
- a multi-period simulator with re-liquidation and depth replenishment.

## Result 1: Reproducing The Linea WETH Cap Decision

The first validation target was LlamaRisk's
[July 2026 reduction of WETH supply and borrow caps](https://governance.aave.com/t/risk-stewards-supply-and-borrow-cap-reductions-on-aave-v3-2026-07-01/25269)
on Aave V3 Linea. The implemented settings verify on-chain at a 6,250 WETH
supply cap and a 2,370 WETH borrow cap.

LlamaRisk reported that WETH reached its liquidation bonus threshold at about
$41,000 of sell size. An independent KyberSwap quote ladder initially
estimated $32,500 on July 18. A refreshed July 30 snapshot at block 31,568,531
moved the estimate to $41,770, almost exactly the reported figure.

![Linea WETH empirical depth](https://raw.githubusercontent.com/BianchiGiacomo/aave-risk-engine/master/docs/assets/linea_weth_depth.png)

The formal clearance test reaches the same risk verdict:

```text
liquidator break-even slippage : 5.66%
largest borrower sale          : $46.44k
max clearable within bonus     : $41.77k -> FAIL
max clearable, 50% haircut     : $20.88k -> FAIL
```

This does not establish that one estimate is uniquely correct. Aggregator
routing and quote time both matter. It does show convergence in magnitude and
decision: Linea WETH instant on-chain depth cannot clear its largest sampled
borrower within the bonus. The full
[case study](https://github.com/BianchiGiacomo/aave-risk-engine/blob/master/docs/case_studies/2026-07-linea-cap-reductions.md)
contains the cap and quote reconciliation.

## Result 2: Mainnet wstETH Is A Concentration Problem

The July 30 Ethereum snapshot at block 25,645,558 separates USD debt from
ETH-denominated leveraged staking loops.

The USD-debt book has a rare but severe simulated loss channel under the
standard two-day calibration:

```text
debt                : $142.35m
positive-loss draws : 5 / 20,000
P(bad debt)         : 0.025% (95% Wilson CI 0.011% to 0.059%)
expected loss       : $3.61k
loss severity       : $14.43m conditional on positive loss
CVaR99              : $360.66k
```

This Monte Carlo result is exploratory: five positive draws identify a rare
but potentially material loss channel, not precise probability, severity, or
CVaR estimates. Expected loss is small because the event is infrequent, while
conditional severity is material. The article's main claim below rests on the
deterministic account and depth comparison rather than those estimates.

The combined book exposes a different tail:

```text
total debt                 : $449.92m
ETH-denominated debt       : $307.37m
P(bad debt)                : 0.11%
CVaR99                     : $20.95m
largest borrower sale      : $307.60m
max clearable within bonus : $2.60m -> FAIL
```

Pure ETH/USD crashes are not the main threat to these loopers because both
collateral and debt fall with ETH. Their relevant channels are the
wstETH/ETH exchange rate and exit depth. One account is more than one hundred
times the estimated instant clearance capacity. In this regime, single-account
concentration is first-order and liquidation mechanics are second-order.

The clearance result is deliberately strict. It measures immediate routed
on-chain exits only. wstETH liquidators can also use the redemption queue over
days, so FAIL should not be read as a claim that eventual recovery is limited
to $2.6 million. It is a claim about immediate clearance under the
[Aave Risk Framework](https://governance.aave.com/t/arfc-aave-risk-framework/25114)
wording.

## Result 3: Mechanics Matter Until Concentration Dominates

The V4 comparison holds the book, scenarios, and depth fixed while changing
repay-to-target sizing, dynamic bonus, close-factor floors, and queue
execution. Linear interpolation between documented bonus anchors is an
explicit modeling assumption.

On the archived July 16 snapshot, before the latest concentration regime
dominated, the mechanics were visible. Relative to V3, the V4 Main
configuration increased the probability of some bad debt from 4.56% to 9.52%,
while reducing VaR99 from $892,000 to $746,000. Larger repay-to-target sales
crossed the participation threshold more often, while deeper liquidations
received a larger bonus and deleveraged more aggressively. Ordered clearing
reduced CVaR by about 20% to 25% because early tranches could still clear
before later sales exhausted depth.

By July 30, the combined-book CVaR was approximately $20.5 million to $21.0
million across V3, V4 Main, V4 correlated, aggregate, and ordered variants.
The whale dominated every mechanics choice.

The multi-period simulator gives another regime distinction. Each one-shot
and evolving row now shares exactly the same terminal scenario. On the current
combined book, V3 CVaR99 was $119.58 million under terminal-only liquidation
and $119.55 million when the book evolved through eight half-day periods. The
whale never meaningfully deleveraged, so path mechanics did not help. On the
archived July 16 book, restoring health factor to 1.24 produced repeat
liquidation in 0.17% of paths, versus 10.72% when restoring only to 1.0137.
Target health factor matters most when liquidation can clear in the first
place.

## Limitations And Next Questions

The borrower scan uses recent Borrow events and can miss dormant accounts.
Real books use an effective single-asset mapping for target-dominant accounts.
The depth curve excludes CEX liquidity and redemption queues. Episode replay
applies historical market paths to today's book rather than reconstructing
historical positions. Results are model outputs, not forecasts.

Two extensions appear most useful:

1. **Lagged peg coupling.** Historical replay shows that the worst peg move
   can lead or lag the worst ETH window. A contemporaneous crash beta can
   place stress at the wrong time.
2. **V4 Hub risk-premium pricing.** Marginal Hub CVaR provides a transparent
   starting point for pricing heterogeneous Spoke credit usage. This is
   especially relevant because the
   [collateral risk premium launched at zero](https://governance.aave.com/t/arfc-aave-v4-activation-on-ethereum-mainnet/24293).

Feedback would be particularly useful on three questions:

1. Should the strict largest-borrower clearance test credit redemption queues,
   and if so, over what horizon?
2. Is bonus-priority ordered clearing a reasonable first approximation to
   competitive liquidator execution?
3. Which V4 dynamic-bonus function should replace linear interpolation between
   the documented anchors?

## Reproduction

```bash
python -m aave_risk_engine.run_market_report
python -m aave_risk_engine.run_market_report --manifest market-report.json
python -m aave_risk_engine.run_market_report --snapshot aave_risk_engine/data/snapshots/aave_v3_linea_weth.json --budget 500000 --figure
python -m aave_risk_engine.run_episode_replay
python -m aave_risk_engine.run_v4_comparison
python -m aave_risk_engine.run_multiperiod
```

The snapshots, episode paths, model code, tests, and complete result
discussion are available in the
[repository](https://github.com/BianchiGiacomo/aave-risk-engine).
