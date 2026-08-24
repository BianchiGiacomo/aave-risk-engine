# Implementation Notes

The package is intentionally small and self-contained. It has no dependency on the original Uniswap V3 RL project.

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Dataclass inputs |
| `stress.py` | Return laws, peg stress, depth haircuts |
| `positions.py` | Synthetic and real borrower-book state, debt denomination, and exposure scaling |
| `liquidation.py` | Liquidation and bad-debt accounting |
| `slippage.py` | Analytic and empirical liquidation-slippage curves |
| `engine.py` | Single-Spoke Monte Carlo engine |
| `multiperiod.py` | Multi-period simulation with book-state evolution |
| `time_to_exit.py` | Deterministic horizon capacity and conditional unresolved-tranche loss |
| `liquidator_balance_sheet.py` | Full-upfront warehouse cash flows, route optimization, capital use, canonical and DEX-market risk, and economic clearance |
| `hub.py` | Multi-Spoke Hub allocator |
| `data/rpc.py` | Stdlib JSON-RPC client with endpoint failover and batching |
| `data/aave_v3.py` | Per-chain Aave V3 readers (Ethereum, Linea): reserves, caps, accounts |
| `data/borrowers.py` | Persistent full-history Borrow-event registries with incremental checkpoints |
| `data/account_cache.py` | Resumable SQLite cache for block-pinned borrower account reads |
| `data/markets.py` | Price history, realized vol, t-tail fit, ARFC peg rule |
| `data/depth.py` | Slippage-curve calibration from Paraswap/KyberSwap sell quotes |
| `data/snapshot.py` | Snapshot schema and JSON persistence |
| `data/book.py` | Snapshot to real PositionBook and calibrated ScenarioConfig |
| `data/clearance.py` | ARFC largest-borrower liquidation clearance test |
| `data/build_snapshot.py` | Live snapshot builder CLI |
| `data/episodes.py` | Historical episodes: paths, cleaning, rolling-window scenarios |
| `data/build_episodes.py` | Episode price-path fetcher CLI |
| `run_market_report.py` | Real-market decision report and reproducibility-manifest CLI |
| `run_episode_replay.py` | Historical stress paths through a selected snapshot book |
| `run_v4_comparison.py` | V3 vs V4 liquidation mechanics on the same book |
| `run_multiperiod.py` | Multi-period stress paths with re-liquidation |
| `run_time_to_exit.py` | DEX refill, redemption, and required-throughput horizon report |
| `run_liquidator_balance_sheet.py` | Liquidator funding, hedge, route, and required-bonus sensitivity report |
| `dashboard.py` | Streamlit dashboard |
| `dashboard_analysis.py` | Cached dashboard adapters for market, clearance-horizon, liquidator-economic, V4, episode, and multi-period analyses |
| `dashboard_charts.py` | Plotly charts, including exit capacity and economic route allocation |
| `plotting.py` | Reproducible Matplotlib report figures |
| `run_demo.py` | Single-Spoke CLI demo |
| `run_hub_demo.py` | Hub allocation CLI demo |

The equations and accounting conventions are collected in
[`docs/model-specification.md`](docs/model-specification.md). Canonical August
18 report inputs and outputs, including snapshot hashes, borrower-registry
coverage, and seeds, are stored in [`docs/manifests/`](docs/manifests/).

## Verification

```bash
python -m aave_risk_engine.tests.test_engine
python -m aave_risk_engine.tests.test_hub
python -m aave_risk_engine.tests.test_data
python -m aave_risk_engine.tests.test_multiperiod
python -m aave_risk_engine.tests.test_time_to_exit
python -m aave_risk_engine.tests.test_liquidator_balance_sheet
```

The tests check:

- slippage calibration and shape,
- correct CVaR tail averaging,
- LTV floor on origination health factor,
- close-factor liquidation sizing,
- cap/LT monotonicity,
- return-law dispatch and tail shape,
- terminal-law preservation, matched multi-period endpoints, and OU peg
  residual variance scaling,
- time-to-exit refill, redemption-delay, throughput, and conditional-loss
  invariants,
- liquidator cash-flow conservation, funding-delay, route-allocation,
  profit-maximizing route selection, canonical-versus-DEX loss separation,
  break-even loss, and minimum-bonus invariants,
- dashboard clearance-extension wiring across horizon, route, and economic
  outputs,
- Hub diversification,
- correlation sensitivity,
- deeper-liquidity allocation,
- severity sensitivity,
- budget binding,
- ABI word decoding and snapshot round-trips,
- full-history borrower-registry updates, validation, and monotonicity,
- block-pinned account reads and interrupted SQLite-cache resumption,
- collateral-enable flags and stable plus variable WETH debt accounting,
- real-book filtering and per-position liquidation thresholds,
- book scaling preserving health factors,
- depth-fit recovery of known liquidity,
- vol / Student-t tail estimators,
- the ARFC peg rule, peg-persistence estimator, and clearance-test math,
- the committed snapshot loading and running offline.

All automated tests are offline; live network paths run only during explicit
snapshot and episode refresh commands.
