# Independent Liquidation-Capacity Stress Tests For Aave

## Scope And Disclosure

> **Independent research prototype.** This work is unaffiliated with Aave
> Labs, the Aave DAO, Chaos Labs, LlamaRisk, or any other Aave service
> provider. It is not a parameter recommendation, automated risk agent, or
> substitute for Risk Steward judgment. The purpose is to offer an
> independent, open-source quantitative lens on Risk Steward decisions and V4
> liquidation design, with assumptions and reproducible code open to
> challenge.

Repository: [BianchiGiacomo/aave-risk-engine](https://github.com/BianchiGiacomo/aave-risk-engine)

Methodology: [METHODOLOGY.md](https://github.com/BianchiGiacomo/aave-risk-engine/blob/v0.1.0/METHODOLOGY.md)

Model equations: [Mathematical model specification](https://github.com/BianchiGiacomo/aave-risk-engine/blob/v0.1.0/docs/model-specification.md)

This note asks a narrow question: when stressed collateral reaches the
liquidation queue, is immediate market depth sufficient to clear the relevant
borrowers within the liquidation bonus?

All real-market results are frozen at the August 17 snapshot blocks stated
below. They are a reproducible research vintage, not a claim about live market
state on the publication date.

## Method

The pipeline uses keyless public sources only: Aave state and account data
from JSON-RPC, routed sell quotes from Paraswap or KyberSwap, ETH history from
Kraken, and LST ratio history from DefiLlama. Recent Borrow events plus known
addresses from the prior snapshot define the candidate universe, which is
then queried on-chain and mapped into real borrower books.
The engine simulates collateral returns, peg moves, and depth evaporation,
places underwater accounts into a liquidation queue, and measures bad debt
with 99% CVaR. Liquidators participate while execution slippage remains below
`bonus / (1 + bonus)`. The engine evaluates both an aggregate queue benchmark
and ordered clearing, which processes positions sequentially by bonus and
seize size, with each cleared tranche consuming depth. The standard market
report retains the aggregate baseline; mechanics comparisons report both
queue rules explicitly. Committed snapshots make the reports deterministic
and offline.

The repository includes:

- deterministic Aave market snapshots and report CLIs;
- V3 and V4 liquidation mechanics with ordered queue clearing;
- historical episode replay on the selected snapshot book;
- a multi-period simulator with re-liquidation and depth replenishment.

## Result 1: Reproducing The Linea WETH Cap Decision

The first validation target was LlamaRisk's
[July 2026 reduction of WETH supply and borrow caps](https://governance.aave.com/t/risk-stewards-supply-and-borrow-cap-reductions-on-aave-v3-2026-07-01/25269)
on Aave V3 Linea. The implemented settings verify on-chain at a 6,250 WETH
supply cap and a 2,370 WETH borrow cap.

LlamaRisk reported that WETH reached its liquidation bonus threshold at about
$41,000 of sell size. An independent KyberSwap quote ladder initially
estimated $32,500 on July 18 and $41,770 on July 30. The August 17 release
snapshot at block 31,741,470 estimates $42,420, still close to the reported
figure.

![Linea WETH empirical depth](https://raw.githubusercontent.com/BianchiGiacomo/aave-risk-engine/v0.1.0/docs/assets/linea_weth_depth.png)

The formal clearance test reaches the same risk verdict:

```text
liquidator break-even slippage : 5.66%
largest borrower sale          : $46.56k
max clearable within bonus     : $42.42k -> FAIL
max clearable, 50% haircut     : $21.21k -> FAIL
```

This does not establish that one estimate is uniquely correct. Aggregator
routing and quote time both matter. It does show convergence in magnitude and
decision: Linea WETH instant on-chain depth cannot clear its largest sampled
borrower within the bonus.

The Linea Monte Carlo run also shows why deterministic evidence matters in a
sparse-loss book. Its only positive draw loses $18,820, but CVaR99 is $94
because that loss is averaged with 199 zeros in the worst 200 draws. The $94
figure is a budget statistic, not an outcome that can occur.

The full
[case study](https://github.com/BianchiGiacomo/aave-risk-engine/blob/v0.1.0/docs/case_studies/2026-07-linea-cap-reductions.md)
contains the cap and quote reconciliation.

[Linea run manifest](https://github.com/BianchiGiacomo/aave-risk-engine/blob/v0.1.0/docs/manifests/linea-weth-2026-08-17.json)

## Result 2: Concentration Dominates The August 17 wstETH Snapshot

The August 17 Ethereum snapshot at block 25,773,934 separates USD debt from
ETH-denominated leveraged staking loops.

The USD-debt book has a rare but severe simulated loss channel under the
standard two-day calibration:

```text
debt                : $343.19m
positive-loss draws : 33 / 20,000
P(bad debt)         : 0.165% (95% Wilson CI 0.118% to 0.232%)
expected loss       : $8.15k
loss severity       : $4.94m conditional on positive loss
CVaR99              : $814.88k
```

The August book is not a clean like-for-like successor to July. Discovery
coverage improved, and 19 addresses absent from July contribute $167.53m of
August debt. Seeding prevents future dropouts but cannot repair July
retroactively. The article's main claim below rests on the deterministic
account and depth comparison.

The combined book exposes a different tail:

```text
total debt                 : $604.47m
ETH-denominated debt       : $261.08m
P(bad debt)                : 0.17%
CVaR99                     : $10.57m
largest borrower sale      : $276.47m
max clearable within bonus : $2.71m -> FAIL
```

Pure ETH/USD crashes are not the main threat to these loopers because both
collateral and debt fall with ETH. Their relevant channels are the
wstETH/ETH exchange rate and exit depth. One account is more than one hundred
times the estimated instant clearance capacity. In this regime, single-account
concentration is first-order and liquidation mechanics are second-order.

The clearance result is deliberately strict. It measures immediate routed
on-chain exits only. wstETH liquidators can also use the redemption queue over
days, so FAIL should not be read as a claim that eventual recovery is limited
to $2.71 million. It is a claim about immediate clearance under the
[Aave Risk Framework](https://governance.aave.com/t/arfc-aave-risk-framework/25114)
wording.

[Ethereum run manifest](https://github.com/BianchiGiacomo/aave-risk-engine/blob/v0.1.0/docs/manifests/ethereum-wsteth-2026-08-17.json)

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
received a larger bonus and deleveraged more aggressively. In these USD-debt
rows, ordered clearing reduced CVaR by about 19% to 23% because early tranches
could still clear before later sales exhausted depth.

The input is the
[archived July 16 snapshot at commit `ac9f2ae`](https://github.com/BianchiGiacomo/aave-risk-engine/blob/ac9f2ae/data/snapshots/aave_v3_ethereum_wsteth.json).
The current CLI with that file and seed 7 reproduces the quoted rows exactly.

By August 17, the combined-book CVaR was approximately $9.95 million to
$10.57 million across V3, V4 Main, V4 correlated, aggregate, and ordered
variants. The whale still dominated every mechanics choice.

The multi-period simulator uses matched terminal scenarios. Over its default
four-day window of eight half-day periods, versus the two-day calibration
above, August 17 combined-book V3 CVaR99 was $45.96 million under
terminal-only liquidation and $45.73 million with book evolution. The whale
never meaningfully deleveraged, so path mechanics did not help. On the
archived July 16 USD-debt book, restoring health factor to 1.24 produced
repeat liquidation in 0.17% of paths, versus 10.72% when restoring only to
1.0137. Target health
factor matters most when liquidation can clear in the first place.

## Primary Governance Question

> Should the largest-borrower clearance requirement use instant routed
> liquidity for redeemable collateral, or should it specify a time horizon
> and recognize primary redemption capacity?

A strict instant test is easy to audit, but it can understate eventual
capacity for collateral with a primary redemption path. A horizon-adjusted
test is more economically complete, but it requires explicit assumptions for
redemption throughput, delay, depth replenishment, and market conditions. The
framework would be clearer if it specified which interpretation governs.

## Limitations And Next Work

Candidate borrowers combine Borrow events in a recent block window with
addresses retained from the prior pinned snapshot. An open account absent
from both sources can still be missed. For each selected target-dominant
account, the single-asset mapping represents its total collateral as the
target asset while retaining its on-chain weighted-average liquidation
threshold. The depth curve excludes CEX liquidity and redemption queues.

Episode replay deliberately asks a counterfactual question: what would the
realized 2022 or 2023 market path do to the August 17, 2026 book? It is not an
archive backtest of the borrowers and liquidity that existed during those
episodes. Results are model outputs, not forecasts.

Two immediate extensions appear most useful:

1. **Time-to-exit.** Replace binary instant clearance with capacity curves at
   explicit horizons, separating DEX replenishment from primary redemption.
2. **Rare-event sampling.** Use stratification or importance sampling to
   estimate low-frequency bad-debt channels without relying on a handful of
   positive brute-force draws.

Lagged peg coupling and V4 Hub risk-premium pricing remain later extensions.
Marginal Hub CVaR is a possible transparent pricing input, particularly
because the
[collateral risk premium launched at zero](https://governance.aave.com/t/arfc-aave-v4-activation-on-ethereum-mainnet/24293),
but no premium recommendation is made here.

Beyond the primary governance question, feedback would be useful on two
modeling choices:

1. Is bonus-priority ordered clearing a reasonable first approximation to
   competitive liquidator execution?
2. Which V4 dynamic-bonus function should replace linear interpolation between
   the documented anchors?

## Reproduction

```bash
git clone https://github.com/BianchiGiacomo/aave-risk-engine
cd aave-risk-engine
pip install -e ".[dev]"

python -m aave_risk_engine.run_market_report --snapshot data/snapshots/aave_v3_ethereum_wsteth.json --manifest docs/manifests/ethereum-wsteth-2026-08-17.json
python -m aave_risk_engine.run_market_report --snapshot data/snapshots/aave_v3_linea_weth.json --budget 500000 --figure --manifest docs/manifests/linea-weth-2026-08-17.json
python -m aave_risk_engine.run_episode_replay
python -m aave_risk_engine.run_v4_comparison
python -m aave_risk_engine.run_multiperiod
python -m streamlit run dashboard.py
```

The dashboard is an optional inspection layer; versioned CLI manifests remain
the canonical evidence. Snapshots, paths, code, tests, and complete results
are available in the
[repository](https://github.com/BianchiGiacomo/aave-risk-engine).
