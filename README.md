# Aave Risk Engine

```text
        ___    ___ _   ________     ____  _________ __ __
      /   |  /   | | / / ____/    / __ \/  _/ ___// //_/
     / /| | / /| | |/ / __/      / /_/ // / \__ \/ ,<
    / ___ |/ ___ | / / /___     / _, _// / ___/ / /| |
   /_/  |_/_/  |_|__/_____/    /_/ |_/___//____/_/ |_|

    LIQUIDATION CAPACITY STRESS ENGINE
```

[![Tests](https://github.com/BianchiGiacomo/aave-risk-engine/actions/workflows/tests.yml/badge.svg)](https://github.com/BianchiGiacomo/aave-risk-engine/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-research%20prototype-orange)

A standalone quantitative research prototype for Aave collateral risk,
liquidation capacity, and V3 to V4 mechanics analysis. It turns pinned Aave V3
reserve state, borrower accounts, oracle prices, and routed depth quotes into
reproducible stress reports, an interactive dashboard, and machine-readable
run manifests.

> Independent research prototype. This project is unaffiliated with Aave
> Labs, the Aave DAO, Chaos Labs, LlamaRisk, or any other Aave service
> provider. It is not a parameter recommendation, production risk system, or
> substitute for Risk Steward judgment.

The committed release evidence is frozen at August 17, 2026. Every headline
number below is tied to a block and snapshot hash; it is not a claim about the
live market on the day this page is read.

![Aave risk engine pipeline](docs/assets/aave_risk_engine_overview.svg)

## What The Project Does

- Loads deterministic snapshots for the wstETH reserve in Aave V3 Ethereum
  Core and the WETH reserve in Aave V3 Linea.
- Builds a USD-debt book and a combined book that revalues ETH-denominated
  debt, exposing LST/WETH looping risk through peg and depth channels.
- Simulates Gaussian, Student-t, and jump-diffusion returns with peg stress,
  depth haircuts, and empirical routed-slippage curves.
- Clears liquidations sequentially in bonus-priority order, using marginal
  slippage on cumulative proceeds. The aggregate queue remains a research
  benchmark in the CLIs.
- Separates cleared-liquidation insolvency from stalled-liquidation recovery
  loss, with liquidator break-even at `bonus / (1 + bonus)`.
- Applies V3 and V4 liquidation mechanics to the same V3 book and common
  scenarios. The V4 result is a mechanics counterfactual, not live V4 data.
- Replays historical ETH and stETH/ETH paths on a selected snapshot book.
- Evolves debt, collateral, repeated liquidation, peg persistence, and depth
  replenishment through matched multi-period paths.
- Reports loss frequency, expected loss, conditional severity, Wilson
  intervals, VaR, CVaR, positive-loss counts, and sparse-tail warnings.
- Exports snapshot hash, parameters, seed, results, cap sweep, and clearance
  evidence as JSON manifests.

Synthetic single-Spoke sizing and V4 Hub allocation remain as secondary
research demos. The real-market reports and dashboard are the primary project
surface.

## Release Evidence

| Snapshot | Block | Date |
|---|---:|---:|
| Aave V3 Ethereum Core, wstETH reserve | 25,773,934 | 2026-08-17 |
| Aave V3 Linea, WETH reserve | 31,741,470 | 2026-08-17 |

The standard market-report values below use the aggregate queue baseline.
The dashboard uses ordered clearing; on the August 17 combined book the queue
choice changes CVaR99 by about 3% to 6% because the result remains
whale-dominated.

| Analysis | Reproduced result |
|---|---|
| Ethereum USD-debt book | `$343.19m` debt; 33 positive losses in 20,000 draws; `P(loss) 0.165%`; `$4.94m` conditional severity; `CVaR99 $814.88k` |
| Ethereum combined book | `$604.47m` debt, including `$261.08m` ETH-denominated; `P(loss) 0.17%`; aggregate `CVaR99 $10.57m`; ordered `CVaR99 $10.07m` |
| Strict wstETH clearance | `$276.47m` largest sale versus `$2.71m` instant clearable within bonus: `FAIL`; redemption and CEX capacity excluded |
| Linea WETH reproduction | `$42.42k` independently clearable versus LlamaRisk's approximately `$41k`; `$46.56k` largest sale: `FAIL` |
| Historical replay | June 2022 worst combined-book window: `$175.84m`; modeled FTX and USDC windows: zero |
| Four-day matched paths | Combined-book V3: `$45.96m` terminal-only CVaR99 versus `$45.73m` evolving-book CVaR99 |

The two-day market-report CVaR and four-day multi-period CVaR are different
horizon conventions and should not be compared as if they were one run.
Sparse-loss results are exploratory: the engine exposes frequency and
conditional severity because CVaR99 may average only a few losses with many
zeros.

The strict clearance test follows the
[Aave Risk Framework](https://governance.aave.com/t/arfc-aave-risk-framework/25114).
The V4 counterfactual uses the governed
[Ethereum activation parameters](https://governance.aave.com/t/arfc-aave-v4-activation-on-ethereum-mainnet/24293).

![Linea WETH empirical depth](docs/assets/linea_weth_depth.png)

The black point is LlamaRisk's `$41k` reference. The orange point is the
`$46.44k` largest sampled borrower sale, above the 5.66% liquidator
break-even line.

See the [consolidated results](docs/results.md), the
[Linea cap-reduction case study](docs/case_studies/2026-07-linea-cap-reductions.md),
and the [governance research note](docs/article/governance-post-draft.md) for
the full evidence and caveats.

## Dashboard

The Streamlit dashboard is a real-market workbench, not the original
synthetic demo.

```bash
python -m streamlit run dashboard.py
```

Sidebar controls select:

- real market: Aave V3 Ethereum Core wstETH or Aave V3 Linea WETH;
- borrower scope: USD debt or Combined;
- minimum target-collateral share;
- 5,000 to 1,000,000 Monte Carlo scenarios;
- random seed and CVaR99 budget;
- committed snapshot, explicit live refresh, restore, and JSON download.

| Tab | Purpose |
|---|---|
| Overview | Reserve exposure, cap usage, tail decomposition, cap sweep, bad-debt distribution, and borrower concentration |
| Sensitivities | Full scenario parameters, loss-driver scatter, empirical depth stress, and fixed-book LT transition or forced-migration sensitivity |
| Clearance | Largest-borrower ARFC test, break-even capacity, quote-range warnings, and stressed clearable depth |
| V3 / V4 | Matched V3, V4 Main, and V4 Correlated mechanics on the selected V3 book with ordered clearing |
| Episodes | Historical ETH and peg paths replayed on the selected snapshot book; available for wstETH |
| Multi-period | Matched terminal shocks versus evolving liquidation paths, with periods, horizon, replenishment, and peg half-life controls |
| Data | Active block, reserve parameters, calibration sources, scan window, and snapshot download |

Expensive analyses run only when their tab button is pressed. A failed live
refresh leaves the committed offline snapshot active. The dashboard never
overwrites committed snapshot files.

## Quick Start

```bash
git clone https://github.com/BianchiGiacomo/aave-risk-engine
cd aave-risk-engine
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m streamlit run dashboard.py
```

macOS or Linux:

```bash
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m streamlit run dashboard.py
```

## Reproduce The Reports

Run from the cloned repository root after installation:

```bash
python -m aave_risk_engine.run_market_report --snapshot data/snapshots/aave_v3_ethereum_wsteth.json --manifest docs/manifests/ethereum-wsteth-2026-08-17.json
python -m aave_risk_engine.run_market_report --snapshot data/snapshots/aave_v3_linea_weth.json --budget 500000 --figure --manifest docs/manifests/linea-weth-2026-08-17.json
python -m aave_risk_engine.run_episode_replay
python -m aave_risk_engine.run_v4_comparison
python -m aave_risk_engine.run_multiperiod
```

Add `--ordered` to `run_market_report` for the dashboard queue convention.
The July 16 V3/V4 mechanics regime is preserved by the
[archived snapshot at commit `ac9f2ae`](https://github.com/BianchiGiacomo/aave-risk-engine/blob/ac9f2ae/data/snapshots/aave_v3_ethereum_wsteth.json).

The original synthetic experiments remain available:

```bash
python -m aave_risk_engine.run_demo
python -m aave_risk_engine.run_hub_demo
```

## Snapshot Refresh

In the dashboard, select a market and open **Snapshot data** in the sidebar:

1. Click **Refresh from public sources**. When the new block appears as
   **Live session**, every dashboard analysis uses that refreshed snapshot.
2. Click **Download active snapshot** to retain the exact JSON used by the
   analysis.
3. Click **Restore committed snapshot** to return to the reproducible August 17
   baseline.

For a saved CLI workflow, build a snapshot into a new file and load that file
with `--snapshot`:

```bash
python -m aave_risk_engine.data.build_snapshot --chain ethereum --asset wstETH --seed-snapshot data/snapshots/aave_v3_ethereum_wsteth.json --out .runtime/ethereum-wsteth-live.json
python -m aave_risk_engine.run_market_report --snapshot .runtime/ethereum-wsteth-live.json --ordered
python -m aave_risk_engine.run_episode_replay --snapshot .runtime/ethereum-wsteth-live.json
python -m aave_risk_engine.run_v4_comparison --snapshot .runtime/ethereum-wsteth-live.json
python -m aave_risk_engine.run_multiperiod --snapshot .runtime/ethereum-wsteth-live.json
```

For Linea, replace the build command with:

```bash
python -m aave_risk_engine.data.build_snapshot --chain linea --asset WETH --blocks 1200000 --seed-snapshot data/snapshots/aave_v3_linea_weth.json --out .runtime/linea-weth-live.json
```

The downloaded dashboard JSON can be supplied to the same CLIs through its
file path. Writing to `.runtime/` keeps the committed release snapshots
unchanged. `--seed-snapshot` retains known borrower addresses that have no
Borrow event inside the new rolling scan window.

Refresh uses public JSON-RPC, Kraken, DefiLlama with a Coingecko fallback,
and routed ParaSwap or KyberSwap quotes. Public endpoints can rate-limit or
reject requests; committed snapshots keep reports and tests deterministic
when that happens.

## Core Conventions

```text
snapshot reserve + target-dominant borrower accounts
  -> USD-debt or combined debt-denomination book
  -> collateral return + peg + executable-depth scenarios
  -> V3 or V4 liquidation sizing
  -> ordered marginal queue or aggregate benchmark
  -> cleared insolvency gaps + stalled delayed-recovery losses
  -> frequency, severity, VaR, CVaR, cap sweep, and manifest
```

- The real-book mapping treats each selected account's total collateral as
  the target asset while retaining its weighted on-chain liquidation
  threshold. The target-share filter limits this approximation.
- Ordered clearing prioritizes bonus, then seize value. Cleared tranches
  consume depth; stalled tranches do not.
- Slippage is a liquidator cost while participation remains profitable. It
  becomes a protocol recovery cost only when liquidation stalls.
- Multi-period runs do not reset price, peg, or the prevailing depth haircut
  after liquidation. Only consumed depth replenishes.
- V4 Main and V4 Correlated parameters are applied to V3 positions as a
  controlled mechanics comparison.

For equations and implementation-level assumptions, read the
[mathematical model specification](docs/model-specification.md),
[methodology](METHODOLOGY.md), and
[implementation notes](IMPLEMENTATION.md).

## Repository Layout

```text
config.py                 model dataclasses and V4 parameters
stress.py                 terminal return laws and matched stress paths
positions.py              borrower book state and exposure scaling
liquidation.py            V3/V4 sizing, ordered clearing, and bad debt
slippage.py               analytic and empirical execution curves
engine.py                 single-period simulation and risk metrics
multiperiod.py            evolving paths and book-state transitions
hub.py                    synthetic V4 Hub allocation experiment
dashboard.py              Streamlit application and snapshot controls
dashboard_analysis.py     cached dashboard analysis adapters
dashboard_charts.py       Plotly dashboard figures
plotting.py               reproducible Matplotlib report figures
data/                     RPC readers, calibration, snapshots, and episodes
docs/                     results, case study, article, model specification
run_market_report.py      market report, depth figure, and manifest export
run_episode_replay.py     historical path replay on a snapshot book
run_v4_comparison.py      matched V3/V4 mechanics comparison
run_multiperiod.py        matched terminal and evolving path comparison
tests/                    economics invariants and offline regressions
```

## Verification

```bash
python -m aave_risk_engine.tests.test_engine
python -m aave_risk_engine.tests.test_hub
python -m aave_risk_engine.tests.test_data
python -m aave_risk_engine.tests.test_multiperiod
```

The current release contains 80 offline tests across engine economics, real
data, multi-period state evolution, and Hub allocation. GitHub Actions runs
the same suites on Python 3.11 and 3.12.

## Honest Limitations

- Borrower discovery combines recent `Borrow` events with the prior snapshot;
  open accounts absent from both sources can still be missed.
- The effective single-asset mapping does not simulate every collateral and
  debt asset separately.
- Instant depth excludes CEX, OTC, and primary redemption capacity.
- Empirical slippage is flat beyond the final quote and is only a lower bound
  there.
- Rare-loss CVaR and conditional severity remain low-sample estimates until
  targeted rare-event sampling is implemented.
- Episode replay is a counterfactual on the selected snapshot book, not an
  archive reconstruction of historical borrowers and liquidity.
- The Hub allocator is synthetic and is not yet connected to live V4 Spokes.

## License

MIT. See [LICENSE](LICENSE).
