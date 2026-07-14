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
