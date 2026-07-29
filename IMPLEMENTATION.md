# Implementation Notes

The package is intentionally small and self-contained. It has no dependency on the original Uniswap V3 RL project.

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Dataclass inputs |
| `stress.py` | Return laws, peg stress, depth haircuts |
| `positions.py` | Synthetic borrower books |
| `liquidation.py` | Liquidation and bad-debt accounting |
| `slippage.py` | Concentrated-liquidity slippage curve |
| `engine.py` | Single-Spoke Monte Carlo engine |
| `hub.py` | Multi-Spoke Hub allocator |
| `data/rpc.py` | Stdlib JSON-RPC client with endpoint failover and batching |
| `data/aave_v3.py` | Per-chain Aave V3 readers (Ethereum, Linea): reserves, caps, accounts |
| `data/markets.py` | Price history, realized vol, t-tail fit, ARFC peg rule |
| `data/depth.py` | Slippage-curve calibration from Paraswap/KyberSwap sell quotes |
| `data/snapshot.py` | Snapshot schema and JSON persistence |
| `data/book.py` | Snapshot to real PositionBook and calibrated ScenarioConfig |
| `data/clearance.py` | ARFC largest-borrower liquidation clearance test |
| `data/build_snapshot.py` | Live snapshot builder CLI |
| `data/episodes.py` | Historical episodes: paths, cleaning, rolling-window scenarios |
| `data/build_episodes.py` | Episode price-path fetcher CLI |
| `run_market_report.py` | Real-market decision report CLI |
| `run_episode_replay.py` | Historical stress paths through today's book |
| `dashboard.py` | Streamlit dashboard |
| `dashboard_charts.py` | Plotly charts |
| `run_demo.py` | Single-Spoke CLI demo |
| `run_hub_demo.py` | Hub allocation CLI demo |

## Verification

```bash
python -m aave_risk_engine.tests.test_engine
python -m aave_risk_engine.tests.test_hub
python -m aave_risk_engine.tests.test_data
```

The tests check:

- slippage calibration and shape,
- correct CVaR tail averaging,
- LTV floor on origination health factor,
- close-factor liquidation sizing,
- cap/LT monotonicity,
- return-law dispatch and tail shape,
- Hub diversification,
- correlation sensitivity,
- deeper-liquidity allocation,
- severity sensitivity,
- budget binding,
- ABI word decoding and snapshot round-trips,
- real-book filtering and per-position liquidation thresholds,
- book scaling preserving health factors,
- depth-fit recovery of known liquidity,
- vol / Student-t tail estimators,
- the ARFC peg rule and clearance-test math,
- the committed snapshot loading and running offline.

All data-layer tests are offline; network code paths run only in
`data/build_snapshot.py`.
