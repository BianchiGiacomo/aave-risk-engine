# Aave Risk Engine

Decision-support tooling for Aave-style collateral risk and V4 Hub-Spoke credit-line sizing.

The project answers two related questions:

1. **Single Spoke:** how large can a borrow cap / credit line be before 99% CVaR bad debt breaches a risk budget?
2. **Hub allocation:** how should one shared Hub balance be split across multiple Spokes with different volatility, liquidity, and correlation?

This is not an automated risk agent and not a governance replacement. It is a compact quant prototype for making risk/growth trade-offs explicit.

## What It Models

```text
borrower book + stressed collateral scenarios
  -> health factors and liquidation queue
  -> liquidator participation threshold: slippage <= bonus / (1 + bonus)
  -> cleared liquidations: protocol loss is insolvency gap
  -> stalled liquidations: protocol marks collateral to delayed executable value
  -> bad-debt distribution
  -> VaR / CVaR / recommended cap
```

For V4-style Hub allocation, one standardized systemic factor drives all Spokes:

```text
u_k = rho_k * Z + sqrt(1 - rho_k^2) * e_k
```

The allocator greedily assigns credit to the Spoke with the lowest marginal Hub-CVaR per dollar until the Hub balance or CVaR budget binds. Marginal premia are approximately equalized because the allocator is discrete.

## Install

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # Windows PowerShell
pip install -e ".[dev]"
```

On macOS/Linux:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

```bash
python -m aave_risk_engine.run_demo
python -m aave_risk_engine.run_hub_demo
python -m streamlit run aave_risk_engine/dashboard.py
```

Tests:

```bash
python -m aave_risk_engine.tests.test_engine
python -m aave_risk_engine.tests.test_hub
```

## Dashboard

The Streamlit dashboard has two tabs:

- **Single Spoke:** reserve-style risk simulation, recommended max-safe cap, slippage/loss/cap/LT charts, and a worst-case scenario inspector.
- **Hub allocation (V4):** editable Spoke table, Hub balance and CVaR budget, systemic return law, severity, credit lines, risk premia, and diversification gain.

Demo tip: in the Hub tab, lower `LONGTAIL`'s `rho`. Its credit line should rise and the diversification gain should increase.

## Repository Layout

```text
aave_risk_engine/
  config.py              dataclass inputs
  stress.py              return laws and stressed scenarios
  positions.py           synthetic borrower book
  liquidation.py         liquidation and bad-debt accounting
  slippage.py            concentrated-liquidity execution shortfall
  engine.py              single-Spoke Monte Carlo engine
  hub.py                 multi-Spoke Hub allocator
  dashboard.py           Streamlit UI
  run_demo.py            single-Spoke demo
  run_hub_demo.py        Hub allocation demo
  tests/                 invariant/economics tests
```

## Honest Limitations

- Synthetic borrower books by default; real Aave account data is not wired in.
- Single-collateral Spokes; no borrower-level cross-collateral portfolios.
- Slippage uses one calibrated concentrated-liquidity curve, not full DEX/CEX routing.
- Hub allocation is a static snapshot with one systemic factor.
- The model is meant for analysis and discussion, not production parameter automation.

See [METHODOLOGY.md](METHODOLOGY.md) for more detail.
