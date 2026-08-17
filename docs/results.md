# Results

This page is the consolidated results narrative for the real-data analyses.
It separates the pinned August 17 market evidence from demonstrations that
depended on an earlier exposure regime.

## Data Vintage

The committed release snapshots were built on August 17, 2026:

| Market | Block |
|---|---:|
| Ethereum mainnet wstETH | 25,773,934 |
| Linea WETH | 31,741,470 |

Borrower discovery combines recent Borrow events with addresses from the
prior pinned snapshot. Dormant accounts absent from both sources may still be
missed. The reports use committed snapshots and run offline.

## 1. Mainnet wstETH Market Report

Command:

```bash
python -m aave_risk_engine.run_market_report --manifest market-report.json
```

Trimmed output from the August 17 mainnet snapshot at block 25,773,934:

```text
USD-debt book entering the engine: 43 accounts | debt $343.19m | collateral $933.76m | median HF 1.89

Tail risk at observed book exposure
  positive draws: 33 / 20,000
  P(bad debt)  : 0.165% (95% Wilson CI 0.118% to 0.232%)
  expected loss: $8.15k
  loss severity: $4.94m conditional on positive loss
  VaR99        : $0
  CVaR99       : $814.88k (worst 200 draws)

Combined book with ETH-denominated debt modeled
  full book: 45 accounts | debt $604.47m (of which ETH-denominated $261.08m)
  P(bad debt) 0.17% | VaR99 $0 | CVaR99 $10.57m

Model-safe debt exposure (CVaR99 budget $5.00m)
  observed book debt : $343.19m
  model safe exposure: $1.03bn
  headroom           : +$686.39m (+200%)

ARFC clearance test (largest borrower within liquidation bonus)
  largest borrower sale          : $276.47m
  largest borrower account       : 0x893aa69fbaa1ee81b536f0fbe3a3453e86290080
  target collateral / debt       : $289.63m / $260.82m
  ETH-denominated debt share     : 100.00%
  caution: the sale exceeds the quote ladder top ($25.00m)
  quiet depth    : slippage >= 74.45% | max clearable $2.71m -> FAIL
```

The USD debt book remains below budget under this exploratory calibration.
The cap sweep reaches 300% of observed exposure while remaining below the 5
million dollar CVaR budget, so it demonstrates at least 200% tested headroom
rather than locating the binding maximum. The August book is not a clean
like-for-like successor to July. Nineteen addresses absent from the July file
contribute $167.53 million of August debt, including two entrants with $64.17
million and $51.90 million. The ordinary August event scan found those
entrants; prior-snapshot seeding only prevents known borrowers from dropping
out. The 2.4 times exposure increase therefore mixes improved discovery
coverage with market movement, and targeted rare-event sampling is still
needed to tighten tail severity.
The combined book tells a different story. Rare peg and depth stress reaches
large ETH debt loopers, producing a 10.57 million dollar CVaR despite only a
0.17% bad debt probability.

The strict ARFC clearance test fails because the largest borrower sale is
about 276 million dollars against 2.71 million dollars of instant clearable
depth. This is an instant routed on-chain test. A wstETH liquidator can also
use the redemption queue over days, which the strict test does not credit.
The 74.45% figure is the last observed Paraswap ladder point and only a lower
bound for the 276.47 million dollar sale, not an extrapolated whale quote.
Large Paraswap ladder points are indicative best routes and not guaranteed
execution.
The main conclusion is therefore about concentration versus immediate exit
capacity, not total eventual recovery capacity.

## 2. Linea WETH Reproduction

The full writeup is the
[July 2026 Linea cap reduction case study](case_studies/2026-07-linea-cap-reductions.md).

Command:

```bash
python -m aave_risk_engine.run_market_report --snapshot data/snapshots/aave_v3_linea_weth.json --budget 500000 --figure
```

Trimmed output from the August 17 Linea snapshot at block 31,741,470:

```text
Aave V3 WETH market report (linea) | block 31,741,470 (2026-08-17)

Tail risk at observed book exposure
  positive draws: 1 / 20,000
  P(bad debt)  : 0.005% (95% Wilson CI 0.001% to 0.028%)
  expected loss: $0.94
  loss severity: $18.82k conditional on positive loss
  VaR99        : $0
  CVaR99       : $94 (worst 200 draws)
  WARNING: fewer than 30 positive-loss draws; CVaR and conditional severity are low-sample estimates

ARFC clearance test (largest borrower within liquidation bonus)
  liquidator break-even slippage : 5.66%
  largest borrower sale          : $46.56k
  quiet depth    : slippage 8.61% | max clearable $42.42k -> FAIL
  stressed depth (50% haircut) : slippage 30.55% | max clearable $21.21k -> FAIL
```

The independent clearance estimate moved from 32.5 thousand dollars on July
18 to 41.8 thousand dollars on July 30 and 42.4 thousand dollars on August
17. Both refreshes remain close to LlamaRisk's approximately 41 thousand
dollar estimate, while the formal clearance verdict remains FAIL because the
largest borrower sale is about 46.6 thousand dollars.

The Linea run also illustrates why sparse-loss reporting needs both frequency
and conditional severity. Its only positive draw loses $18.82k, while CVaR99
is $94 because the other 199 draws in the worst 1% are zero. No simulated
outcome loses $94: it is an unconditional risk-budget statistic, not event
severity.

## 3. Historical Episode Replay

Command:

```bash
python -m aave_risk_engine.run_episode_replay
```

This replay uses the August 17 mainnet book at block 25,773,934. It applies
historical price and peg paths to that snapshot's positions and depth. It does
not reconstruct historical borrower books.

```text
Historical episode replay | wstETH (ethereum) block 25,773,934 | horizon 2d

Episode: ftx-2022 (2022-11-01 to 2022-11-30)
  combined book :  quiet depth: max bad debt        $0  stressed depth: max bad debt        $0

Episode: steth-depeg-2022 (2022-05-01 to 2022-06-29)
  combined book :  quiet depth: max bad debt  $175.84m (2022-06-15)  stressed depth: max bad debt  $175.84m (2022-06-15)
  driving windows (combined, quiet): 2022-06-15 (eth -11.2%, peg 5.65%) $175.84m

Episode: usdc-depeg-2023 (2023-02-28 to 2023-03-20)
  combined book :  quiet depth: max bad debt        $0  stressed depth: max bad debt        $0
```

The June 2022 peg window activates the whale channel and produces 175.84
million dollars of bad debt on the August 17 combined book. The FTX and USDC
windows are clean for the modeled collateral channels. The timing matters:
the worst realized peg moves need not coincide with the worst ETH return
windows. A contemporaneous crash beta can therefore overstate or misplace
peg stress, which motivates a lagged coupling model.

## 4. V3 And V4 Liquidation Mechanics

Command:

```bash
python -m aave_risk_engine.run_v4_comparison
```

The result is regime dependent. The July 16 snapshot at block 25,546,280
provided a useful mid-size liquidation regime. It showed the mechanics
tradeoff directly:

```text
USD-debt book ($146.42m)
  V3 (on-chain params)  aggregate: P(bad debt)  4.56% | mean  $42.38k | VaR99  $892.29k | CVaR99    $1.66m
  V3 (on-chain params)  ordered  : P(bad debt)  4.56% | mean  $38.57k | VaR99  $892.29k | CVaR99    $1.28m
  V4 Main Spoke         aggregate: P(bad debt)  9.52% | mean  $38.44k | VaR99  $746.25k | CVaR99    $1.62m
  V4 Main Spoke         ordered  : P(bad debt)  9.52% | mean  $35.40k | VaR99  $746.25k | CVaR99    $1.32m
```

V4 Main increased the frequency of some loss because repay-to-target sold
more collateral near the threshold, but it reduced severity beyond the loss
threshold. In the two quoted USD-debt rows, ordered clearing reduced CVaR by
19% to 23% because early tranches could clear before later positions exhausted
depth. Across all six archived V3 and V4 book comparisons, the reduction was
13% to 27%.

The July 16 snapshot is recoverable as
`data/snapshots/aave_v3_ethereum_wsteth.json` at commit `ac9f2ae`. Pass that
JSON to the current CLI through `--snapshot` to reproduce the older block.

The August 17 snapshot at block 25,773,934 is a different regime:

```text
combined book ($604.47m)
  V3 (on-chain params)  aggregate: P(bad debt)  0.17% | mean $105.66k | VaR99        $0 | CVaR99   $10.57m
  V3 (on-chain params)  ordered  : P(bad debt)  0.17% | mean $100.67k | VaR99        $0 | CVaR99   $10.07m
  V4 Main Spoke         aggregate: P(bad debt)  0.17% | mean $105.57k | VaR99        $0 | CVaR99   $10.56m
  V4 Main Spoke         ordered  : P(bad debt)  0.17% | mean $102.02k | VaR99        $0 | CVaR99   $10.20m
  V4 correlated Spoke   aggregate: P(bad debt)  0.07% | mean $105.45k | VaR99        $0 | CVaR99   $10.55m
  V4 correlated Spoke   ordered  : P(bad debt)  0.07% | mean  $99.48k | VaR99        $0 | CVaR99    $9.95m
```

One whale remains about 102 times larger than instant clearable depth, so V3
versus V4 and aggregate versus ordered clearing remain secondary on the
combined book. The apparent
contradiction is the result: liquidation mechanics matter in the middle,
while concentration dominates after exposure crosses the available exit
capacity by two orders of magnitude.

## 5. Multi-Period Simulation

Command:

```bash
python -m aave_risk_engine.run_multiperiod
```

The single-shock and evolving rows share the exact terminal return, peg drop,
and depth haircut on every path. This CLI uses a four-day window, longer than
the two-day market-report calibration above. The August 17 mainnet snapshot
at block 25,773,934 shows what happens when whale concentration dominates:

```text
combined book ($604.47m)
  V3                single: P(bad debt)  0.76% | mean $459.63k | CVaR99   $45.96m
  V3                multi : P(bad debt)  0.43% | mean $457.28k | CVaR99   $45.73m | marks 100% of losses | events/path 0.04 | P(reliq) 0.19%
```

The CVaR results are almost identical because the dominant whale cannot clear
and deleveraging barely occurs. Matching endpoints removes the previous apparent
2.3 times reduction, which came primarily from constructing a four-day
Student-t shock differently from the sum of eight half-day Student-t shocks.
Path mechanics cannot help a position that remains far beyond available depth.

The July 16 mid-size regime at block 25,546,280 exposed the target health
factor mechanism that the August 17 whale regime masks on the USD-debt book:

```text
V4 Main (1.24)    single: P(bad debt) 17.50% | mean  $99.03k | CVaR99    $3.47m
V4 Main (1.24)    multi : P(bad debt) 10.75% | mean  $68.69k | CVaR99    $2.93m | marks 100% of losses | events/path 0.26 | P(reliq) 0.17%
V4 corr (1.0137)  single: P(bad debt)  5.58% | mean  $51.38k | CVaR99    $2.56m
V4 corr (1.0137)  multi : P(bad debt)  0.32% | mean  $12.62k | CVaR99    $1.26m | marks 100% of losses | events/path 0.59 | P(reliq) 10.72%
```

Restoring health factor to 1.24 almost eliminated repeat liquidation.
Restoring only to 1.0137 left positions close enough to the boundary that
10.72% of paths liquidated a position again. This is why target health
factor should be evaluated with an evolving book rather than only a
terminal shock.

## Interpretation

Across the five analyses, market structure is the first-order result. The
pinned USD debt exposure can look safe while a correlated whale remains
unclearable through immediate market depth. Liquidation design changes the
distribution materially when queues are comparable with depth, but no
mechanics choice can compensate for a single account that is more than one
hundred times the instant clearance capacity.

## Synthetic Demo Figure Guide

The synthetic single Spoke figures are generated by:

```bash
python -m aave_risk_engine.run_demo
```

### Credit-Line Decision

![CVaR budget vs credit line](assets/cap_budget.png)

The red curve shows 99% CVaR bad debt as the borrow cap or Spoke credit line
increases. The green horizontal line is the risk budget. The vertical marker
is the largest modeled exposure that stays inside that budget. The curve
bends upward because larger liquidation queues create worse execution
shortfall and more stalled-liquidation risk.

### Liquidation Capacity

![Liquidation slippage curve](assets/slippage_curve.png)

The slippage curve maps liquidation notional to execution shortfall. The
horizontal line is liquidator break-even, `bonus / (1 + bonus)`. Below it,
the liquidator can absorb execution cost. Above it, liquidation incentives
fail and the protocol is marked to delayed executable recovery.

### Bad-Debt Tail

![Bad debt distribution](assets/loss_distribution.png)

Most scenarios create no bad debt. CVaR measures the average loss in the
worst 1% of scenarios, so it captures severity beyond the VaR threshold.

### Hub-Spoke Allocation

The Hub example is generated by:

```bash
python -m aave_risk_engine.run_hub_demo
```

The allocator adds credit to the Spoke with the lowest marginal Hub CVaR per
dollar until the Hub balance or risk budget binds. Lower-correlation Spokes
can receive credit even when they are risky alone because they diversify the
Hub tail.
