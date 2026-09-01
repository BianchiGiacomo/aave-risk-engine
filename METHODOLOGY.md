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

## Queue Clearing

Two clearing models are available:

- **Aggregate**: one queue-average slippage for the whole
  liquidation queue, compared per position against its own break-even
  `bonus / (1 + bonus)`. Fast, but all-or-nothing: when the average
  crosses break-even, the entire queue stalls at once. It remains an
  optional research benchmark and CLI compatibility mode.
- **Ordered** (`ordered_queue`): the queue clears sequentially in
  bonus-priority order (seize size breaking ties, mirroring liquidator
  profit priority). Each tranche is assessed at its marginal slippage on
  the cumulative proceeds curve; cleared tranches consume depth, stalled
  tranches do not, and a stalled position is marked at the slippage its
  own sale would have realized. This is the dashboard execution model.

The queue effect is regime dependent. On the August 18 complete combined
book, ordered clearing reduces CVaR99 by 52% for V3, 18% for V4 Main, and 32%
for V4 Correlated because early tranches can clear before later positions
exhaust depth. Ordered clearing is still single-period: depth does not
replenish between tranches, and no follow-on liquidations occur after the
window.

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
- **Borrower book**: a persistent registry covers `Borrow` events from the
  configured Pool proxy deployment through the pinned snapshot block. Every
  historical candidate is re-queried with `Pool.getUserAccountData` at that
  block, and accounts above the minimum current-debt floor are stored. Target
  collateral and WETH stable plus variable debt are read at the same block.
  Each account enters the book under an *effective single-asset mapping*: its
  total collateral is treated
  as the target asset and shocked by the scenario price, using the account's
  own on-chain weighted-average liquidation threshold. This is the
  perfectly-correlated-collateral view; restricting to accounts whose
  collateral is dominated by the target asset (default share >= 50%) keeps
  the approximation honest.
- **Depth**: observed routed sell quotes are used two ways. The engine
  interpolates the (notional, slippage) points directly, because real exit
  liquidity can fall off a cliff once concentrated pools are exhausted
  (observed for wstETH: near zero at $2m, 25% at $8m, and 74% at $25m),
  which no single-`L`
  curve represents. A scenario depth haircut `h` acts as size scaling,
  `s(Q, h) = s_quiet(Q / (1 - h))`, an identity under the analytic curve.
  A median-fitted `L` reference point is kept for components needing a
  smooth curve. Slippage is measured against the smallest-size quote rate,
  not an external oracle. Quotes the router rejects under its max-impact
  guard still carry a routed amount; those are treated as indicative
  stress-depth, not guaranteed executable liquidity. Beyond the largest
  quoted size the interpolation is flat, which understates losses there,
  so ladders should extend past the sizes that matter.
- **Debt denomination**: each account's WETH-denominated debt is measured
  on-chain. In the default USD-shock book, accounts whose debt is mostly
  WETH-denominated (leveraged staking loops) are excluded, because their
  debt leg falls with ETH-correlated collateral in a USD crash. The
  combined book instead models the split explicitly: the ETH-denominated
  portion of scenario debt scales with the ETH return, so a pure ETH/USD move
  revalues both legs while stable-debt accounts keep the full USD price shock.
  Under the live mainnet wstETH oracle, the relative trigger is canonical-rate
  impairment; the secondary-market peg terms are only its counterfactual proxy,
  as detailed below. Exit depth matters once liquidation begins. The report
  shows both debt views.
- **Peg channel versus the live oracle**: the peg series is the
  secondary-market LST/underlying ratio, but Aave's mainnet wstETH feed is an
  exchange-rate feed. At block 25,780,402 the oracle price equals Lido's
  `stEthPerToken()` times ETH/USD when calculated with unrounded inputs
  (rounded values: rate 1.24188444, ETH/USD $1,898.3475, wstETH feed
  $2,357.5282). A secondary-market stETH discount therefore does not move the
  feed and does not by itself trigger liquidation. The peg terms are a
  counterfactual proxy for canonical-rate impairment, such as slashing, or for
  a change of oracle design. They are not a calibrated estimate of
  present-oracle liquidation frequency or severity. Withdrawal-queue
  congestion affects exit time and secondary liquidity instead. Conditional
  on liquidation, the clearance test still measures the relevant exit depth.
- **Peg persistence**: refreshed pegged-asset snapshots estimate a no-intercept
  AR(1) coefficient on cleaned below-par deviations and convert it to a daily
  OU speed. The August 18 Ethereum snapshot calibrates `0.0961/day`, equivalent
  to a 7.22-day half-life. The published four-day multi-period results include
  this mean reversion. It changes pathwise residual variance relative to a
  random-walk assumption but does not change the matched terminal law.
- **Return law**: annualized realized volatility from daily closes; the
  Student-t degrees of freedom are matched to sample excess kurtosis
  (`dof = 4 + 6/k`, clamped to [2.6, 12]) when tails are heavy. Assets with
  no calibrated peg series (WETH, WBTC) get all peg-stress terms zeroed:
  they are their own underlying, so exchange-rate stress does not apply.
- **Exposure sweeps** on a real book scale debt and collateral together,
  preserving the observed health-factor distribution.

## Multi-Period Simulation

The multi-period simulator divides the stress window into periods and
evolves the book through them, removing three single-period conventions
at once:

- **Stalls wait**: a stalled liquidation is not marked to fire-sale value
  immediately; it waits, and a price recovery can rescue it. Only
  positions still under water at the end of the window are marked to
  delayed executable value.
- **Re-liquidation**: cleared repayments and seizures update the book, so
  a position restored to the V4 target health factor can be liquidated
  again if subsequent cumulative shocks push it below HF 1. Market
  conditions do not reset after a clear. On the August 18 USD-debt book, the
  full V4 Main configuration produces re-liquidation in 0.27% of matched
  paths, versus 16.93% for V4 Correlated. Their target health factors, 1.24
  and 1.0137, contribute to this gap, but the configurations also differ in
  bonus and close-factor-floor parameters.
- **Depth replenishment**: depth consumed by cleared sales carries into
  the next period scaled by `1 - replenish`. Configurations that clear
  many small tranches are the most sensitive to slow replenishment.

Every comparison uses matched endpoints: the single-shock and evolving rows
share the exact terminal return, peg drop, and depth haircut on each path.
Gaussian and jump-diffusion returns use coherent increments. Student-t paths
use one shared variance mixture per path, so subdividing the horizon preserves
the configured terminal Student-t law instead of thinning its tail. The peg
drop is the clipped sum of base stress, crash beta times running ETH drawdown,
and an OU idiosyncratic residual. If `kappa` is the daily speed and `dt` the
period length, that residual follows `x[t] = exp(-kappa * dt) * x[t-1] + eps[t]`.
Innovation variance is scaled so the peg residual retains its calibrated
variance at the configured horizon. At `kappa = 0` this reduces exactly to the
previous random walk. The depth idiosyncratic term remains a random walk.

Differences between the rows therefore come from liquidation timing and book
evolution, not different terminal scenario distributions. Intermediate clears
can deleverage positions, stalled positions can recover, and consumed depth can
replenish. These effects need not reduce losses. Early partial clears can also
pay bonuses and consume collateral and depth without creating enough health
factor buffer, making the evolving result worse than terminal-only liquidation.
When a whale never clears, the single-shock and evolving results can be almost
identical. With one period and identical shocks the simulator reproduces the
single-period ordered engine exactly (tested).

## Rare-Event Reporting

CVaR99 remains the risk-budget convention: it averages the worst 1% of all
draws, including zero-loss draws when bad debt occurs in less than 1% of the
simulation. In a sparse-loss run this is a valid unconditional risk measure,
but it is not event severity. Linea makes the distinction concrete: six
positive draws in 20,000 have conditional severity of $82.02k, while CVaR99
is $2.46k because 194 zeros also enter the worst 1% average. The value is
mathematically correct as an unconditional budget statistic but easy to
misread as event severity. The market report therefore also exposes:

```text
expected loss = P(loss) * E[loss | loss > 0]
```

It prints the positive-loss count, both factors in that decomposition, and a
95% Wilson interval for `P(loss)`. CVaR and conditional severity are flagged as
low-sample estimates below 30 positive draws. The Wilson interval does not
solve severity uncertainty, and repeated seeds are a variance diagnostic, not
a substitute for additional or better-targeted tail samples. Publication-grade
work on basis-point loss probabilities should use a validated stratified or
importance-sampling estimator.

`run_market_report --manifest PATH` writes a JSON record containing the exact
snapshot SHA-256, block, seed, calibrated parameters, results, cap sweep, and
clearance test.

## V4 Liquidation Mechanics

With V4 parameters set, liquidation follows the design live on mainnet
since March 2026 instead of the V3 baseline:

- **Repay-to-target**: the repayment R restores the position to the
  Spoke's target health factor, solving
  `(C - R(1+b)) LT = target (D - R)`. When the target is unreachable
  (`target <= (1+b) LT`), the position is closed entirely. A close-factor
  floor (0.60 volatile, 0.35 correlated at launch) sets the minimum
  repayable fraction, and positions that would be left below the dust
  threshold are closed in full.
- **Dynamic bonus**: `liquidationBonusFactor * maxLiquidationBonus` just
  below par (0.90 x 1.11x-V3 on Main Spoke, so about 6.0%), rising to the
  full max bonus at or below `healthFactorForMaxBonus` (0.90 Main, 0.99
  correlated). The anchors are the documented activation parameters; the
  linear rise between them is this model's assumption, since the engine
  overview specifies a Dutch-auction-style increase without a formula.
- **Per-position stalls**: each position stalls at its own break-even
  `s > b_i / (1 + b_i)`, so deep-in-default positions keep clearing at
  slippage levels that stall near-par liquidations.

On the August 18 complete book, mechanics and queue execution are both
visible. Aggregate combined-book CVaR99 is $31.82m for V3, $32.85m for V4
Main, and $32.83m for V4 Correlated; ordered clearing reduces those values to
$15.25m, $26.87m, and $22.41m. These are joint configuration comparisons,
not isolated estimates of one parameter. The multi-period simulator is needed
to show how restoring HF to 1.0137 versus 1.24 affects vulnerability to
follow-on shocks.

## Historical Episode Replay

Past stress episodes are replayed as deterministic scenarios: every rolling
stress-horizon window of the realized ETH price and stETH/ETH ratio paths
becomes one scenario, evaluated against the selected snapshot book and depth
curve. This is scenario replay, not backtesting against the historical book,
which would require archive-node state that keyless endpoints do not serve.
It answers: what would those market paths do to the positions in the selected
snapshot?

Data handling for the daily price series (keyless DefiLlama marks):

- gappy series are reindexed onto a full calendar grid by interpolation,
  so an H-day window always spans H calendar days;
- the LST/underlying ratio is capped at par, because minting enforces a
  hard ceiling at 1.0 and prints above it are venue or timestamp
  artifacts (the 2022 series prints up to 1.12 on chaotic days);
- a three-point rolling median removes remaining single-mark spikes.

Episodes are named after the historical event but carry only the
collateral-relevant paths (ETH price, stETH/ETH ratio); liability-side
effects such as the March 2023 USDC depeg itself are outside the replay.

The replay helps separate loss channels. In the August 18 book, the June 2022
peg window drives $42.20m of combined-book loss even though the corresponding
two-day ETH return is positive. The stressed-depth FTX window produces a much
smaller $381.42k loss, and the modeled USDC window produces none. This exposes
a limitation of contemporaneous Monte Carlo peg coupling: realized peg stress
can lead or lag the largest ETH price move. Because that window is peg-driven,
it should be read through the oracle caveat above: on today's exchange-rate
feed it represents a counterfactual canonical-rate impairment of the same
magnitude, not the 2022 secondary-market discount itself.

## ARFC Checks

Two requirements of the Aave Risk Framework (governance ARFC, June 2026)
are evaluated directly:

- **Peg rule**: for pegged collateral, no deviation of more than 1% below peg
  sustained for two days or longer (E-Mode precondition). The comparison is
  strict, matching the framework wording; a deviation resting exactly on 1%
  passes.
- **Liquidation capacity**: secondary-market depth must clear the largest
  expected borrower within the liquidation bonus. In this model that is the
  liquidator break-even condition `s(Q) <= bonus / (1 + bonus)` evaluated at
  `Q = min(collateral, debt * (1 + bonus))` for the largest account, under
  quiet and stressed depth. The break-even is hit at `Q* = sqrt(P) * L * bonus`,
  which the report quotes as maximum clearable notional.

## Time-To-Exit Sensitivity

The strict ARFC result is extended from an instant verdict to explicit
horizons. Let `C0` be instant clearable DEX capacity and `tau` the assumed time
for one equivalent refill. Cumulative DEX capacity at hour `t` is:

```text
C_dex(t) = C0 * (1 + t / tau)
```

Under stress, the depth haircut reduces `C0` and a separate, longer refill time
is used. If primary redemption starts after delay `d` with throughput `R` per
day:

```text
C_redemption(t) = R * max(0, t - d) / 24
C_total(t) = C_dex(t) + C_redemption(t)
```

The report evaluates instant, one-hour, six-hour, one-day, three-day, and
seven-day horizons. It also solves for the minimum `R` needed to clear by each
horizon, which is the decision-relevant output when live redemption capacity
is unknown.

If sale notional `U` remains unresolved, it corresponds to debt
`U / (1 + bonus)`. Under an additional collateral drawdown `delta`, the
conditional stalled-tranche mark is:

```text
L_unresolved = max(0, U / (1 + bonus) - U * (1 - delta))
```

This is not a forecast of whole-account bad debt. Refill time, redemption
delay, and throughput are transparent sensitivities rather than measurements
of live Lido, CEX, or OTC capacity.

## Liquidator Warehouse Economics

The strict instant test remains unchanged. A separate economic-clearance test
asks whether a liquidator can repay debt at time zero, warehouse the seized
collateral, hedge ETH/USD, and exit over the selected DEX and redemption
capacity paths. For repaid debt `D`, bonus `b`, and seized collateral value
`Q = D * (1 + b)`, each exited tranche `q_j` realizes:

```text
DEX_recovery = q_DEX
             * (1 - canonical_loss)
             * (1 - DEX_market_discount)
             * (1 - DEX_execution_loss)

redemption_recovery = q_redemption
                    * (1 - canonical_loss)
                    * (1 - redemption_loss)
```

The ETH/USD exposure is assumed hedged. `canonical_loss` represents an
impairment between the liquidation valuation and final canonical recovery,
such as a canonical-rate or oracle-to-recovery loss, so it affects both exit
routes. `DEX_market_discount` is a secondary-market discount and affects
only collateral sold through the DEX. Keeping these channels separate
prevents a temporary wstETH/ETH market discount from being charged to
collateral that is instead redeemed at its canonical value. DEX and
redemption capacities are independent route ceilings, but collateral is
assigned only once, so their sum cannot double count the seized amount.
`DEX_market_discount` must represent an additional valuation discount, not
the same price impact already charged through `DEX_execution_loss`; calibrating
both to one observed quote would double count the loss.

The time-zero cash balance is debt repayment plus hedge-entry and fixed costs.
While it remains negative, funding accrues on the outstanding cash deficit.
Hedge carry accrues on unresolved collateral. Capital employed is the integral
of the cash deficit over time, and the return hurdle is charged against that
capital-days measure. Economic profit is:

```text
economic_profit = realized_recovery
                - debt_repaid
                - funding_cost
                - hedge_entry_cost
                - hedge_carry_cost
                - fixed_cost
                - capital_hurdle
```

Economic clearance passes only if all collateral exits within the maximum
horizon and economic profit is non-negative. The report also solves for the
minimum bonus, the largest canonical loss, and the largest DEX-only market
discount consistent with that test.
This is a proposed horizon-adjusted incentive criterion, not a replacement for
the Risk Framework's strict instant routed-depth requirement.

Funding and hurdle both accrue on the same capital-days measure, so the rates
add economically. The default 10% funding rate plus 10% hurdle is a 20% annual
economic capital charge: funding is the modeled cash expense and the hurdle is
the additional required return. They remain separate in the report so their
roles and dollar contributions are visible.

The default route strategy numerically maximizes economic profit over one
total DEX allocation `x`, with `Q - x` assigned to redemption. Feasible route
capacity at the maximum horizon bounds the search:

```text
max(0, Q - C_redemption(T)) <= x <= min(Q, C_DEX(T))
x_star = argmax_x economic_profit(x)
```

For each candidate split, both assigned routes execute at their earliest
available modeled capacity. A coarse grid brackets the best region and a
bounded scalar search refines it. Collateral is assigned only once. The
`capacity_first` CLI strategy remains available as a benchmark that consumes
both routes as soon as capacity appears.

This remains a full-upfront warehouse strategy, so peak capital is at least
the selected debt repayment before costs. It does not establish that this
capital, flash liquidity, hedge size, or future primary-redemption capacity is
available. The optimizer selects a static total split against deterministic
capacity curves; it is not an adaptive or stochastic execution controller.
V3 close factors may split repayment across transactions. A 1% DEX
execution-loss ceiling gives a different instant capacity from the strict ARFC
ceiling of `bonus / (1 + bonus)`; the two reports must not be compared as if
their capacity threshold were identical.

Quiet and stressed redemption throughput are separate inputs. The default
stress value equals the quiet value to preserve a matched-throughput comparison,
but this independence is not a claim about market behavior. DEX depth and the
Lido withdrawal queue may deteriorate together. A lower stressed-redemption
input is therefore the appropriate sensitivity until joint stress is calibrated.

## RWA Drawdown And Permissioned Liquidators

The RWA proxy analysis treats a four-session return as close-to-close across
five dated observations. It reports the worst unconditional window and the
worst forward window whose first observation is already below an inclusive
rolling maximum by a selected drawdown threshold. Sweeping every rolling
lookback from 20 through 250 sessions tests whether the conditional result is
an artifact of one chosen peak window.

HYG adjusted close is committed solely as a reproducible high-yield
market-price proxy. It is not HINC NAV and does not reproduce the J.P. Morgan
CLOIE Post-BB component. A worst-month scaling transfers the disclosed HINC
blend to HYG monthly-loss ratio onto the HYG four-session loss. That output is
a heuristic bracket, not an estimate, because relative volatility need not be
constant across horizons or stress regimes.

For a lump redemption after `d` calendar days, recovery loss `l`, funding rate
`f`, and hurdle rate `h`, the normalized minimum economic bonus is:

```text
minimum_bonus = (1 + (f + h) * d / 365) / (1 - l) - 1
```

This calculation separates three quantities. Gross financing is the debt
repaid at time zero. Loss-absorbing capital covers the mark-to-recovery move.
The minimum bonus compensates that loss plus the cost of capital. A 3% to 5%
backstop statement is incomplete unless it identifies which quantity it
means and the simultaneous repayment notional against which it is measured.

Permissioned collateral makes these distinctions more binding. Aave fixes the
bonus ex ante in either design, but a permissionless market allows another
profitable liquidator to enter. A whitelist removes that fallback during
stress. A common daily NAV print can also move several positions through their
threshold together. Exact capacity sizing therefore requires position-level
LTV and liquidation-threshold data, the close-factor rule, committed
stablecoin financing, and redemption terms.

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
