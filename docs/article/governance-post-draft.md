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

All primary results are frozen at the August 18, 2026 blocks stated below.
Earlier July and August 17 snapshots used incomplete rolling borrower
discovery. They are not used for current book-level claims; the July Linea
quote ladders remain useful because depth measurement does not depend on the
borrower sample.

## Method

The pipeline uses keyless public sources: Aave JSON-RPC state, Paraswap or
KyberSwap routed quotes, Kraken prices, and DefiLlama LST ratios. A persistent
registry replays every `Borrow` event from each configured Pool deployment,
then re-queries all historical candidates at one pinned block. Candidates
still carrying at least $10,000 of debt at that block are stored in the
snapshot. Target-collateral-share and debt-denomination filters then construct
the USD-debt and combined analysis books. The engine simulates collateral
returns, peg moves, and depth evaporation; clears liquidations sequentially by
bonus and seize size; and reports loss frequency, expected loss,
conditional severity, VaR, and CVaR. Liquidators participate while slippage is
below `bonus / (1 + bonus)`. Snapshots, hashes, seeds, and parameters are
committed for offline reproduction.

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

The independent depth estimate agrees with LlamaRisk in magnitude. Complete
borrower discovery also finds a current sale far above that threshold, so a
formal largest-borrower clearance test reaches the same risk direction as the
cap reduction.

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
million aggregate-queue CVaR99. Its deterministic concentration test is:

```text
largest borrower sale      : $256.52m
max clearable within bonus : $2.73m -> FAIL
```

The sale is about 94 times estimated instant clearance capacity. That
borrower's debt is entirely ETH-denominated, so it is a leveraged staking
loop: its debt falls with its collateral, and it is stressed by the
wstETH/ETH exchange rate and by exit depth rather than by the USD price of
ETH. Its collateral still sells on the same depth curve once liquidated,
which is why the clearance test includes it. This is not a claim that wstETH
is unsafe or that eventual recovery is limited to $2.73 million. The strict
test includes routed on-chain exits only; wstETH can also be redeemed over
time, and CEX or OTC liquidity may exist. It exposes an interpretation
problem in the
[Aave Risk Framework](https://governance.aave.com/t/arfc-aave-risk-framework/25114):
instant depth and horizon liquidation capacity are not the same quantity.

[Ethereum run manifest](https://github.com/BianchiGiacomo/aave-risk-engine/blob/v0.1.0/docs/manifests/ethereum-wsteth-2026-08-18.json)

## Result 3: Mechanics Matter, But They Do Not Create Depth

The V4 comparison holds the V3 book, scenarios, and depth fixed while changing
repay-to-target sizing, dynamic bonus, close-factor floors, dust rules, and
queue execution. V4 Main is the general-asset Spoke configuration, repaying a
liquidated position to health factor 1.24 with a 60% close-factor floor, a
0.90 bonus factor, and a 0.90 health-factor anchor for maximum bonus. V4
Correlated is the tight-band configuration for assets that track each other,
such as an LST against ETH, repaying to 1.0137 with a 35% floor, a 1.0 bonus
factor, and a 0.99 maximum-bonus anchor. Both use a modeled maximum bonus 1.11
times the V3 bonus. This is a mechanics counterfactual, not live V4 borrower
data. Linear interpolation between documented bonus anchors is an explicit
model assumption.

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

Historical replay provides a separate deterministic stress lens. Applying
the June 2022 ETH and stETH/ETH path to today's combined book produces a
$42.20 million worst window. This is scenario replay on today's positions,
not reconstruction of the historical book.

## Primary Governance Question

> Should the largest-borrower clearance requirement use instant routed
> liquidity for redeemable collateral, or should it specify a time horizon
> and recognize primary redemption capacity?

A strict instant test is auditable but can understate eventual capacity. A
horizon-adjusted test is economically richer but requires explicit assumptions
for redemption throughput, delay, depth replenishment, and market conditions.
The framework would be clearer if it specified which interpretation governs.

## Limitations And Next Work

The effective single-asset mapping represents each selected account's total
collateral as the target asset while retaining its weighted on-chain
liquidation threshold. It does not separately shock every collateral balance.
The snapshot stores borrowers above a $10,000 debt floor. Empirical depth is
flat beyond the final quote and is only a lower bound there. Episode replay is
not archive backtesting, and plain Monte Carlo remains inefficient for very
rare losses.

The next two extensions are time-to-exit, separating DEX replenishment from
primary redemption at explicit horizons, and rare-event sampling with
uncertainty diagnostics. Lagged peg coupling and V4 Hub risk-premium pricing
remain later work; no premium recommendation is made here.

Feedback would be especially useful on whether bonus-priority ordered
clearing is a reasonable first approximation to competitive execution, and
which V4 dynamic-bonus function should replace linear interpolation between
the documented anchors.

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
