# Independent Liquidation-Capacity Stress Tests For Aave

## Scope And Disclosure

> **Independent research prototype.** This work is unaffiliated with Aave
> Labs, the Aave DAO, Chaos Labs, LlamaRisk, or any other Aave service
> provider. It is not a parameter recommendation, automated risk agent, or
> substitute for Risk Steward judgment. Its purpose is an open-source
> quantitative lens on Risk Steward decisions and V4 liquidation design, with
> assumptions and code open to challenge.

Repository: [BianchiGiacomo/aave-risk-engine](https://github.com/BianchiGiacomo/aave-risk-engine)

Methodology: [METHODOLOGY.md](https://github.com/BianchiGiacomo/aave-risk-engine/blob/v0.1.0/METHODOLOGY.md)

Model equations: [Mathematical model specification](https://github.com/BianchiGiacomo/aave-risk-engine/blob/v0.1.0/docs/model-specification.md)

This note asks a narrow question: when stressed collateral reaches the
liquidation queue, is immediate market depth sufficient to clear the relevant
borrowers within the liquidation bonus?

Primary results are frozen at the August 18, 2026 blocks below. Earlier books
used incomplete borrower discovery and are not used for current exposure
claims; their quote ladders remain valid depth observations.

## Summary

- **Reproduced decision.** A KyberSwap ladder puts Linea WETH's maximum
  clearable size at $43,195, close to LlamaRisk's approximately $41,000 July
  estimate.
- **Mainnet concentration.** The largest wstETH borrower would, if liquidated,
  put a $256.52 million sale into a market whose instant routed depth clears
  $2.73 million within the bonus, about 94 times smaller. That is a statement
  about immediate on-chain exits, not about eventual recovery.
- **Policy question.** Should the Aave Risk Framework's clearance requirement
  be read against instant routed depth, or against a horizon-adjusted capacity
  that credits primary redemption and depth replenishment?

## Method

The pipeline uses keyless Aave JSON-RPC state, routed quotes, Kraken prices,
and DefiLlama LST ratios. A persistent registry replays `Borrow` events and
re-queries every candidate at one pinned block. Accounts with at least $10,000
of debt enter the snapshot; target-share and debt-denomination filters form the
USD-debt and combined books. The engine simulates returns, relative-value
moves, and depth evaporation, then clears liquidations sequentially by bonus
and seize size. Liquidators participate while slippage is below
`bonus / (1 + bonus)`. Versioned snapshots and manifests preserve inputs,
hashes, seeds, parameters, and results.

The repository includes deterministic market reports, V3 and V4 liquidation
mechanics, ordered queue clearing, historical episode replay, multi-period
book evolution, and an interactive real-data dashboard.

## Result 1: Reproducing The Linea WETH Cap Decision

The first validation target was LlamaRisk's
[July 2026 WETH cap reduction](https://governance.aave.com/t/risk-stewards-supply-and-borrow-cap-reductions-on-aave-v3-2026-07-01/25269)
on Aave V3 Linea. The implemented settings verify on-chain at a 6,250 WETH
supply cap and a 2,370 WETH borrow cap.

LlamaRisk reported that WETH reached its liquidation-bonus threshold at about
$41,000 of sell size. Independent KyberSwap ladders estimated $32,500 on July
18, $41,770 on July 30, and $43,195 in the August 18 release snapshot at block
31,749,322.

![Linea WETH empirical depth](https://raw.githubusercontent.com/BianchiGiacomo/aave-risk-engine/v0.1.0/docs/assets/linea_weth_depth.png)

```text
liquidator break-even slippage : 5.66%
largest borrower sale          : $300.47k
max clearable within bonus     : $43.20k -> FAIL
max clearable, 50% haircut     : $21.60k -> FAIL
```

The estimate agrees with LlamaRisk in magnitude. Complete discovery also finds
a current sale above the threshold, so the formal clearance test supports the
same risk direction as the cap reduction.

The Monte Carlo row shows why frequency and severity must accompany CVaR in a
sparse book: six positive draws have mean severity of $82,020, while CVaR99
is $2,460 because 194 zeros enter the same worst-1% average.

[Linea case study](https://github.com/BianchiGiacomo/aave-risk-engine/blob/v0.1.0/docs/case_studies/2026-07-linea-cap-reductions.md) | [Run manifest](https://github.com/BianchiGiacomo/aave-risk-engine/blob/v0.1.0/docs/manifests/linea-weth-2026-08-18.json)

## Result 2: Mainnet wstETH Concentration Versus Instant Depth

The Ethereum snapshot at block 25,780,402 covers 84,427 historical borrower
candidates and stores 9,526 accounts with at least $10,000 of current debt.
The target-dominant USD-debt book is near the exploratory $5 million CVaR
budget under the standard two-day calibration:

```text
debt                : $634.47m
positive-loss draws : 110 / 20,000
P(bad debt)         : 0.550% (95% Wilson CI 0.457% to 0.662%)
expected loss       : $47.85k
loss severity       : $8.70m conditional on positive loss
CVaR99              : $4.78m
```

The combined book, which revalues $329.46 million of ETH-denominated debt,
has $964.28 million of total debt, 3.64% bad-debt probability, and $31.82
million aggregate-queue CVaR99. Its deterministic concentration test, which
measures instant routed capacity only, is:

```text
largest borrower sale      : $256.52m
max clearable within bonus : $2.73m -> FAIL
```

The sale is about 94 times instant capacity. The borrower is a leveraged
staking loop with entirely ETH-denominated debt, so an ETH/USD move revalues
both legs; relative wstETH/ETH value and exit depth are the relevant channels.

The liquidation trigger requires care. At the snapshot block Aave's wstETH
feed matches Lido's canonical wstETH/stETH rate times ETH/USD: rounded inputs
are 1.24188444, $1,898.3475, and $2,357.5282. A secondary-market stETH/ETH
discount does not by itself reduce this feed or the borrower's health factor.
Because the model calibrates its relative-value factor on that market ratio,
peg-driven losses are counterfactual canonical-rate impairment scenarios, not
calibrated liquidation probabilities under the live oracle.

That channel is not hypothetical. Chaos Labs' March 2026
[post-mortem on wstETH exchange-rate misalignment](https://governance.aave.com/t/post-mortem-exchange-rate-misallignment-on-wsteth-core-and-prime-instances/24269)
reports a CAPO parameter mismatch that lowered the effective exchange rate by
about 2.85% and liquidated roughly 10,938 wstETH across 34 accounts on the
Core and Prime instances, the same Core instance this snapshot reads. Slashing
or an adapter fault can impair the canonical rate; withdrawal-queue congestion
instead affects exit time and secondary liquidity. Conditional on liquidation,
the clearance test is unchanged because seized wstETH still reaches the routed
market.

`FAIL` is therefore a conditional instant-capacity result, not a live bad-debt
estimate or a claim that wstETH is unsafe. Redemption over time and CEX or OTC
liquidity may increase eventual capacity. This exposes an interpretation issue
in the
[Aave Risk Framework](https://governance.aave.com/t/arfc-aave-risk-framework/25114):
instant depth and horizon liquidation capacity are not the same quantity.

[Ethereum run manifest](https://github.com/BianchiGiacomo/aave-risk-engine/blob/v0.1.0/docs/manifests/ethereum-wsteth-2026-08-18.json)

## Result 3: Mechanics Matter, But They Do Not Create Depth

The V4 comparison holds the V3 book, scenarios, and depth fixed. V4 repays to
a target health factor; modeled V3 repays 50%, or 100% below HF 0.95. Main uses
target HF 1.24, a 60% close-factor floor, 0.90 bonus factor, and 0.90
maximum-bonus HF; Correlated uses 1.0137, 35%, 1.0, and 0.99. Both use a
maximum bonus 1.11 times V3. This is a mechanics counterfactual, not live V4
borrower data, and linear interpolation between bonus anchors is an assumption.

On the current combined book, aggregate CVaR99 is $31.82 million for V3,
$32.85 million for V4 Main, and $32.83 million for V4 Correlated. Ordered
clearing reduces those values to $15.25 million, $26.87 million, and $22.41
million respectively. Early transactions can clear before later sales consume
depth, so queue execution is material. No mechanics choice, however, makes a
$256.52 million sale fit inside $2.73 million of instant capacity.

Matched four-day paths also separate terminal stress from evolving
liquidation. Combined-book V3 CVaR99 is $32.78 million under a terminal-only
shock and $31.85 million with book evolution. V4 Main moves from $51.08
million to $47.84 million; V4 Correlated moves from $40.76 million to $39.68
million. Re-liquidation occurs in 0.21% of V4 Main paths versus 16.84% of V4
Correlated paths. The higher repay target contributes to that contrast, but
bonus and floor parameters also differ, so it is not a single-parameter
causal estimate.

Applying the June 2022 ETH and stETH/ETH path to today's combined book produces
a $42.20 million worst window. Because the historical loss driver was a market
discount, the oracle-consistent reading is a counterfactual canonical-rate
impairment of the same magnitude. This replays market paths on today's
positions; it does not reconstruct the historical book.

## Primary Governance Question

> Should the largest-borrower clearance requirement use instant routed
> liquidity for redeemable collateral, or should it specify a time horizon
> and recognize primary redemption capacity?

A strict instant test is auditable but can understate eventual capacity. A
horizon-adjusted test is economically richer but requires explicit assumptions
for redemption throughput, delay, depth replenishment, and market conditions.
The framework would be clearer if it specified which interpretation governs.

## Limitations And Next Work

The single-asset mapping applies the target shock to all collateral in selected
accounts while retaining each weighted liquidation threshold. Empirical depth
is a lower bound beyond the final quote. Episode replay is not archive
backtesting, and plain Monte Carlo is inefficient for rare losses. Most
importantly, secondary-market peg calibration does not estimate canonical-rate
impairment probabilities under the current wstETH oracle.

Next work is time-to-exit, separating DEX replenishment from redemption, and
rare-event sampling with uncertainty diagnostics. A direct canonical-rate
model should accompany it. V4 Hub premium pricing remains later work; no
premium recommendation is made here.

## Discussion And Feedback Requested

Feedback from Risk Stewards and service providers would be useful on:

1. Is bonus-priority ordered clearing a reasonable first approximation to
   competitive liquidator execution?
2. Which V4 dynamic-bonus function should replace linear interpolation between
   the documented anchors?
3. For exchange-rate-oracled collateral, is a canonical-rate impairment
   scenario the right way to stress the correlated-asset channel, and what
   magnitude would be considered plausible?

## Reproduction

```bash
git clone https://github.com/BianchiGiacomo/aave-risk-engine
cd aave-risk-engine
pip install -e ".[dev]"

python -m aave_risk_engine.run_market_report --snapshot data/snapshots/aave_v3_ethereum_wsteth.json --manifest docs/manifests/ethereum-wsteth-2026-08-18.json
python -m aave_risk_engine.run_market_report --snapshot data/snapshots/aave_v3_linea_weth.json --budget 500000 --figure --manifest docs/manifests/linea-weth-2026-08-18.json
python -m aave_risk_engine.run_episode_replay
python -m aave_risk_engine.run_v4_comparison
python -m aave_risk_engine.run_multiperiod
python -m streamlit run dashboard.py
```

The dashboard is an optional inspection layer; versioned CLI manifests remain
the canonical evidence.
