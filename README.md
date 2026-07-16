# Aave Risk Engine

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-research%20prototype-orange)

Decision-support tooling for Aave-style collateral risk and V4 Hub-Spoke credit-line sizing.

> Independent research prototype. This project is not affiliated with, endorsed by, or sponsored by Aave Labs or the Aave DAO.

![Aave-style risk engine flow](docs/assets/aave_risk_flow.svg)

The project answers two related questions:

1. **Single Spoke:** how large can a borrow cap / credit line be before 99% CVaR bad debt breaches a risk budget?
2. **Hub allocation:** how should one shared Hub balance be split across multiple Spokes with different volatility, liquidity, and correlation?

This is not an automated risk agent and not a governance replacement. It is a compact quant prototype for making risk/growth trade-offs explicit.

## 60-Second Tour

- Sizes a borrow cap / V4 Spoke credit line from 99% CVaR bad debt.
- Treats liquidation slippage as a liquidator cost while incentives work, and as a protocol recovery cost only when liquidations stall.
- Models fat-tailed and jump-diffusion stress, peg widening, and liquidity-depth evaporation.
- Allocates one shared Hub balance across Spokes by marginal Hub-CVaR.
- Shows why lower-correlation Spokes can receive credit even when they look risky standalone.

## Read Next

- [Results walkthrough](docs/results.md)
- [Methodology](METHODOLOGY.md)
- [Implementation notes](IMPLEMENTATION.md)

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

## Results Preview

### Single-Spoke Credit-Line Sizing

The chart below is the core decision view. It sweeps the borrow cap / Spoke credit line and reports the resulting 99% CVaR bad debt. The recommended cap is the largest exposure that stays inside the chosen risk budget.

![CVaR budget vs credit line](docs/assets/cap_budget.png)

### Liquidation Capacity

Liquidators clear positions only while execution slippage is below break-even:

```text
slippage <= bonus / (1 + bonus)
```

Below that line, slippage is a liquidator cost compensated by the bonus. Above it, liquidations stall and the protocol marks collateral to delayed executable value.

![Liquidation slippage curve](docs/assets/slippage_curve.png)

### Loss Distribution

Most scenarios have no bad debt. The relevant risk is concentrated in the far right tail, which is why the engine uses CVaR rather than only VaR.

![Bad debt distribution](docs/assets/loss_distribution.png)

For a fuller walkthrough, see [docs/results.md](docs/results.md).

### Hub Allocation Example

The Hub demo allocates a `$700m` USDC Hub balance across three Spokes under an `$8m` CVaR budget.

| Spoke | rho | Credit line | Standalone CVaR | Risk premium / $1m |
|---|---:|---:|---:|---:|
| stETH | 0.95 | $70m | $2.0m | $48.5k |
| WBTC | 0.85 | $315m | $4.2m | $79.4k |
| LONGTAIL | 0.45 | $105m | $2.6m | $61.6k |

In this run, total credit is `$490m` (`70%` of Hub balance), Hub CVaR is `$7.9m`, and diversification benefit is about `$0.9m`. The exact numbers vary with Monte Carlo seed and allocation granularity; the qualitative story is the point.

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
