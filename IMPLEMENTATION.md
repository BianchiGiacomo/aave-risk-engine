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
| `dashboard.py` | Streamlit dashboard |
| `dashboard_charts.py` | Plotly charts |
| `run_demo.py` | Single-Spoke CLI demo |
| `run_hub_demo.py` | Hub allocation CLI demo |

## Verification

```bash
python -m aave_risk_engine.tests.test_engine
python -m aave_risk_engine.tests.test_hub
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
- budget binding.
