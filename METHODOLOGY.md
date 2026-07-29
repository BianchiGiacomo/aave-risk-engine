# Methodology

## Single-Spoke Risk Budgeting

For borrower `i` with debt `D_i`, collateral value `C_i`, and liquidation threshold `LT`:

```text
HF_i = C_i * LT / D_i
```

Positions with `HF < 1` enter the liquidation queue.

The liquidation bonus is treated economically:

```text
liquidator break-even: s <= bonus / (1 + bonus)
```

where `s` is execution shortfall versus oracle. If liquidation clears, the liquidator absorbs slippage and the protocol loss is only the insolvency gap:

```text
bad_debt = max(0, D - C / (1 + bonus))
```

If liquidation stalls, the protocol marks collateral to delayed executable value:

```text
bad_debt = max(0, D - C * (1 - s) * (1 - delay))
```

This is a conservative stress convention. Real Aave does not literally self-liquidate immediately; bad debt remains until a later liquidation, repayment, or resolution path.

## Slippage

Liquidation slippage uses a concentrated-liquidity closed form:

```text
s(Q) = q / (L + q),  q = Q / sqrt(P)
```

Fractional slippage is increasing and concave; dollar shortfall `Q * s(Q)` is convex. That convexity makes tail loss grow faster than exposure once liquidation queues become large.

## Return Laws

The terminal collateral return can be:

- `student_t`: default symmetric fat-tailed return.
- `jump_diffusion`: Merton diffusion plus downward-skewed compound-Poisson jumps for gap risk.
- `gaussian`: thin-tailed reference.

The same downside draw also drives peg stress and depth evaporation.

## Real-Data Calibration

When a market snapshot is available, model inputs come from observation
rather than assumption:

- **Risk parameters** (LT, LTV, bonus) are read from the Aave V3
  `PoolDataProvider`; the spot price from the Aave oracle (USD, 8 decimals).
- **Borrower book**: accounts are discovered from recent `Borrow` events and
  aggregated with `Pool.getUserAccountData`. Each account enters the book
  under an *effective single-asset mapping*: its total collateral is treated
  as the target asset and shocked by the scenario price, using the account's
  own on-chain weighted-average liquidation threshold. This is the
  perfectly-correlated-collateral view; restricting to accounts whose
  collateral is dominated by the target asset (default share >= 50%) keeps
  the approximation honest.
- **Depth**: observed routed sell quotes are used two ways. The engine
  interpolates the (notional, slippage) points directly, because real exit
  liquidity can fall off a cliff once concentrated pools are exhausted
  (observed for wstETH: ~0.3% at $2m, >50% at $9m), which no single-`L`
  curve represents. A scenario depth haircut `h` acts as size scaling,
  `s(Q, h) = s_quiet(Q / (1 - h))`, an identity under the analytic curve.
  A median-fitted `L` reference point is kept for components needing a
  smooth curve. Slippage is measured against the smallest-size quote rate,
  not an external oracle. Quotes the router rejects under its max-impact
  guard still carry a routed amount; those are treated as indicative
  stress-depth, not guaranteed executable liquidity. Beyond the largest
  quoted size the interpolation is flat, which understates losses there,
  so ladders should extend past the sizes that matter.
- **Debt denomination**: accounts whose debt is mostly WETH-denominated
  (leveraged staking loops) are excluded from the USD-shock book. Their
  debt leg falls with ETH-correlated collateral in a USD crash, so
  stable-debt scenarios do not describe them; their residual risk is the
  LST exchange rate, not the price level. The report states the excluded
  volume explicitly.
- **Return law**: annualized realized volatility from daily closes; the
  Student-t degrees of freedom are matched to sample excess kurtosis
  (`dof = 4 + 6/k`, clamped to [2.6, 12]) when tails are heavy.
- **Exposure sweeps** on a real book scale debt and collateral together,
  preserving the observed health-factor distribution.

## ARFC Checks

Two requirements of the Aave Risk Framework (governance ARFC, June 2026)
are evaluated directly:

- **Peg rule**: for pegged collateral, no deviation of 1% or more below peg
  sustained for two days or longer (E-Mode precondition).
- **Liquidation capacity**: secondary-market depth must clear the largest
  expected borrower within the liquidation bonus. In this model that is the
  liquidator break-even condition `s(Q) <= bonus / (1 + bonus)` evaluated at
  `Q = min(collateral, debt * (1 + bonus))` for the largest account, under
  quiet and stressed depth. The break-even is hit at `Q* = sqrt(P) * L * bonus`,
  which the report quotes as maximum clearable notional.

## Hub Allocation

A V4-style Hub aggregates liquidity and allocates credit lines to Spokes. The model uses one systemic factor `Z` and one idiosyncratic factor per Spoke:

```text
u_k = rho_k * Z + sqrt(1 - rho_k^2) * e_k
```

The systemic factor is standardized; it sets tail shape. The Hub `severity` parameter scales each Spoke's volatility and controls stress magnitude.

The allocator greedily adds one credit increment to the Spoke that increases Hub CVaR least. It stops when the Hub CVaR budget or Hub balance binds. Because the allocation is greedy and discrete, marginal risk premia are approximately equalized rather than exactly equalized.

Diversification benefit is:

```text
sum(standalone Spoke CVaR) - Hub CVaR
```

CVaR subadditivity implies this should be non-negative up to Monte Carlo noise.
