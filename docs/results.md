# Results

This page is the consolidated narrative for the real-data analyses. All
primary results use the complete borrower registries and block-pinned August
18, 2026 snapshots committed with the release.

## Data Vintage

| Market | Block | Borrow registry | Active debt accounts stored |
|---|---:|---:|---:|
| Ethereum mainnet wstETH | 25,780,402 | 84,427 candidates | 9,526 |
| Linea WETH | 31,749,322 | 13,397 candidates | 51 |

The registry covers every `Borrow` event from the configured Aave V3 Pool
proxy deployment through the snapshot block. Every historical candidate is
then re-queried at that one block; the snapshot stores all accounts with at
least $10,000 of current debt. The target-share and debt-denomination filters
are applied later when a report constructs a book.

The earlier July and August 17 snapshots used rolling event discovery plus a
prior-snapshot seed. They remain useful historical artifacts, but their
borrower-book totals are not comparable with this release. Direct reserve
state and quote ladders do not depend on borrower discovery.

## 1. Mainnet wstETH Market Report

Command:

```bash
python -m aave_risk_engine.run_market_report --snapshot data/snapshots/aave_v3_ethereum_wsteth.json --manifest docs/manifests/ethereum-wsteth-2026-08-18.json
```

Trimmed output from block 25,780,402:

```text
borrower registry: blocks 16,291,127 to 25,780,402 | 84,427 candidates | 9,526 with debt >= $10.00k

USD-debt book entering the engine: 790 accounts | debt $634.47m | collateral $1.67bn | median HF 1.94

Tail risk at observed book exposure
  positive draws: 110 / 20,000
  P(bad debt)  : 0.550% (95% Wilson CI 0.457% to 0.662%)
  expected loss: $47.85k
  loss severity: $8.70m conditional on positive loss
  VaR99        : $0
  CVaR99       : $4.78m (worst 200 draws)

Combined book with ETH-denominated debt modeled
  full book: 855 accounts | debt $964.28m (of which ETH-denominated $329.46m)
  P(bad debt) 3.64% | VaR99 $5.49m | CVaR99 $31.82m

Model-safe debt exposure (CVaR99 budget $5.00m)
  observed book debt : $634.47m
  model safe exposure: $657.66m
  headroom           : +$23.19m (+4%)

ARFC clearance test (largest borrower within liquidation bonus)
  largest borrower sale          : $256.52m
  largest borrower account       : 0x893aa69fbaa1ee81b536f0fbe3a3453e86290080
  target collateral / debt       : $273.24m / $242.00m
  ETH-denominated debt share     : 100.00%
  caution: the sale exceeds the quote ladder top ($25.00m)
  quiet depth    : slippage >= 74.10% | max clearable $2.73m -> FAIL
```

The corrected USD-debt book is close to, but still within, the exploratory
$5 million CVaR budget. The 110 positive draws support the frequency estimate
better than prior vintages, while conditional severity remains a model output
that should be stress-tested with targeted tail sampling.

The combined book exposes the LST looper channel. Its tail is materially
larger because peg and depth stress can reach ETH-denominated debt positions.
The deterministic clearance test is stricter still: the largest sale is
about 94 times the instant clearable amount. The 74.10% slippage is the final
Paraswap ladder point and only a lower bound beyond $25 million. The test
excludes CEX, OTC, and wstETH redemption capacity, so FAIL is a statement
about immediate routed liquidity rather than eventual recovery.

## 2. Linea WETH Reproduction

The full writeup is the
[July 2026 Linea cap reduction case study](case_studies/2026-07-linea-cap-reductions.md).

Command:

```bash
python -m aave_risk_engine.run_market_report --snapshot data/snapshots/aave_v3_linea_weth.json --budget 500000 --figure --manifest docs/manifests/linea-weth-2026-08-18.json
```

Trimmed output from block 31,749,322:

```text
borrower registry: blocks 12,430,836 to 31,749,322 | 13,397 candidates | 51 with debt >= $10.00k

USD-debt book entering the engine: 22 accounts | debt $887.88k | collateral $4.41m | median HF 1.55

Tail risk at observed book exposure
  positive draws: 6 / 20,000
  P(bad debt)  : 0.030% (95% Wilson CI 0.014% to 0.065%)
  expected loss: $25
  loss severity: $82.02k conditional on positive loss
  VaR99        : $0
  CVaR99       : $2.46k (worst 200 draws)
  WARNING: fewer than 30 positive-loss draws; CVaR and conditional severity are low-sample estimates

ARFC clearance test (largest borrower within liquidation bonus)
  liquidator break-even slippage : 5.66%
  largest borrower sale          : $300.47k
  quiet depth    : slippage 72.28% | max clearable $43.20k -> FAIL
  stressed depth (50% haircut) : slippage 83.32% | max clearable $21.60k -> FAIL
```

The independent maximum-clearance estimate progressed from $32.5 thousand on
July 18 to $41.8 thousand on July 30, $42.4 thousand on August 17, and $43.2
thousand on August 18. It remains close to LlamaRisk's approximately $41
thousand estimate. Complete discovery also finds a much larger current
borrower than the rolling scan did, strengthening the same deterministic
FAIL verdict.

This run illustrates sparse-loss reporting. Six losses have conditional
severity of $82.02 thousand, while CVaR99 is $2.46 thousand because 194 zeros
also enter the worst 1% average. CVaR is a valid unconditional budget measure
here, but it is not event severity and is weakly estimated from six events.

## 3. Historical Episode Replay

Command:

```bash
python -m aave_risk_engine.run_episode_replay
```

This is scenario replay on the August 18 book, not reconstruction of the
borrower books and liquidity that existed during each episode.

```text
Historical episode replay | wstETH (ethereum) block 25,780,402 | horizon 2d
books USD $634.47m | combined $964.28m (ETH $329.46m)

Episode: ftx-2022
  USD-debt book : quiet depth $236 | stressed depth $381.42k
  combined book : quiet depth $236 | stressed depth $381.42k

Episode: steth-depeg-2022
  USD-debt book : quiet depth $374 | stressed depth $439.76k
  combined book : quiet depth $42.20m | stressed depth $42.20m
  driving window: 2022-06-16 (ETH +1.8%, peg 4.93%) $42.20m

Episode: usdc-depeg-2023
  USD-debt and combined books: $0
```

The June 2022 peg window activates the ETH-looper channel even though its
two-day ETH return is positive. This is the timing result: realized peg stress
can lead or lag the largest ETH price move, while a contemporaneous crash-beta
model forces the channels together. The FTX stressed-depth loss is small
relative to the book and the USDC episode is outside the modeled collateral
channels.

## 4. V3 And V4 Liquidation Mechanics

Command:

```bash
python -m aave_risk_engine.run_v4_comparison
```

The CLI applies alternative mechanics to the same V3 positions and common
random scenarios. It is a counterfactual, not observed V4 borrower data.

```text
USD-debt book ($634.47m)
  V3                  aggregate: P 0.55% | mean $47.85k | CVaR99 $4.78m
  V3                  ordered  : P 0.55% | mean $28.41k | CVaR99 $2.84m
  V4 Main             aggregate: P 0.64% | mean $49.43k | CVaR99 $4.94m
  V4 Main             ordered  : P 0.64% | mean $36.17k | CVaR99 $3.62m
  V4 Correlated       aggregate: P 0.64% | mean $44.13k | CVaR99 $4.41m
  V4 Correlated       ordered  : P 0.64% | mean $22.57k | CVaR99 $2.26m

combined book ($964.28m)
  V3                  aggregate: P 3.64% | mean $361.46k | CVaR99 $31.82m
  V3                  ordered  : P 3.64% | mean $189.29k | CVaR99 $15.25m
  V4 Main             aggregate: P 3.64% | mean $404.53k | CVaR99 $32.85m
  V4 Main             ordered  : P 3.64% | mean $342.37k | CVaR99 $26.87m
  V4 Correlated       aggregate: P 3.64% | mean $403.24k | CVaR99 $32.83m
  V4 Correlated       ordered  : P 3.64% | mean $291.98k | CVaR99 $22.41m
```

Ordered clearing is material in the complete book. It reduces combined-book
CVaR by 52% for V3, 18% for V4 Main, and 32% for V4 Correlated because early
tranches clear before later transactions consume the remaining depth. V4
Main and Correlated refer to the governed parameter sets, including their
target health factors, bonus anchors, close-factor floors, and dust rules.
The differences are joint mechanics effects, not estimates from live V4
positions.

## 5. Multi-Period Simulation

Command:

```bash
python -m aave_risk_engine.run_multiperiod
```

The default comparison uses eight half-day periods over four days, 100% depth
replenishment between periods, and a calibrated 7.22-day peg-residual
half-life. Each single-shock and evolving row shares the same terminal return,
peg drop, and depth haircut path by path.

```text
combined book ($964.28m)
  V3             single: P  8.91% | mean $591.02k | CVaR99 $32.78m
  V3             multi : P 12.72% | mean $578.30k | CVaR99 $31.85m | P(reliq) 4.64%
  V4 Main        single:                         CVaR99 $51.08m
  V4 Main        multi :                         CVaR99 $47.84m | P(reliq) 0.21%
  V4 Correlated  single:                         CVaR99 $40.76m
  V4 Correlated  multi :                         CVaR99 $39.68m | P(reliq) 16.84%
```

The previous two-to-three-times gap disappears once the terminal shocks are
matched correctly. Evolving liquidation now reduces combined-book CVaR by
about 3% for V3, 6% for V4 Main, and 3% for V4 Correlated, although it can
raise the probability of some loss while reducing severity. The
re-liquidation contrast remains informative: restoring positions toward HF
1.24 creates more buffer than restoring toward 1.0137, but
the rows also differ in bonus and floor parameters, so the comparison is not
a pure target-HF experiment.

## Interpretation

The corrected borrower universe changes the quantitative narrative. The USD
book is near the chosen budget rather than comfortably below it, the combined
tail is larger, and ordered execution materially changes CVaR. The two most
robust governance observations are deterministic: the independent Linea depth
estimate continues to reproduce LlamaRisk's figure, and the Ethereum largest
borrower remains far beyond instant routed depth. For redeemable collateral,
that second result motivates a time-to-exit model rather than a claim that
eventual recovery is impossible.

## Synthetic Demo Figure Guide

The synthetic single-Spoke figures are generated by:

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
