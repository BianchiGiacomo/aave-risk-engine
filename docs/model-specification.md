# Mathematical Model Specification

## Purpose And Scope

This document is the mathematical contract for the Aave risk engine. It
describes the equations implemented in the repository, the accounting
conventions behind reported bad debt, and the boundaries of the current
model. It is not a production risk policy or a parameter recommendation.

The engine has four related components:

1. a single-period Monte Carlo model for one target collateral reserve;
2. deterministic largest-borrower clearance tests;
3. an evolving multi-period liquidation simulator;
4. a synthetic V4 Hub allocation model.

The real-market reports use committed Aave V3 snapshots. A V4 comparison
applies alternative V4 liquidation rules to the same V3 borrower book and
the same stress scenarios. It is therefore a mechanics counterfactual, not
an observation of a V4 borrower book.

## 1. Borrower Book

For borrower $i$, let:

- $D_i$ be debt in USD at time zero;
- $U_i$ be target-collateral units;
- $P_t$ be the stressed target-collateral price;
- $C_{i,t} = U_i P_t$ be collateral value;
- $LT_i$ be the account's on-chain weighted-average liquidation threshold.

The health factor is

$$
HF_{i,t} = \frac{C_{i,t}LT_i}{D_{i,t}}.
$$

An account is liquidatable when $HF_{i,t} < 1$.

Real books use an effective single-asset mapping. The account's complete
collateral value is represented as target-asset units, while its own
weighted-average $LT_i$ is retained. Accounts must meet a minimum target
collateral share and a minimum debt size. This keeps the mapping focused on
target-dominant accounts, but it does not model correlated shocks to each
non-target collateral balance separately.

The candidate universe is a persistent set of every `Borrow` event
`onBehalfOf` address observed from the configured Pool proxy deployment through
the snapshot block. Every candidate is re-queried at that same block. The
snapshot stores accounts whose current total debt exceeds the configured
floor, which is $10,000 for the release evidence. Target collateral is counted
only when its reserve-level collateral flag is enabled for that user.

For the combined book, debt may have a stable and an ETH-denominated part:

$$
D_{i,t} = D^{stable}_i + D^{ETH}_i(1 + R^{ETH}_t).
$$

The USD-debt book excludes accounts whose ETH-denominated debt share exceeds
50%. The combined book retains them. Consequently, a wstETH/WETH loop is
primarily exposed to the wstETH/ETH exchange rate and liquidation depth, not
to a pure ETH/USD move.

When the engine tests a different aggregate exposure, debt, collateral, and
ETH debt are scaled by the same factor. This preserves each account's health
factor and concentration weights.

## 2. Terminal Stress Model

Let $H$ be the liquidation horizon in years and $X_H$ the ETH log return.
The simple ETH return is

$$
R^{ETH}_H = \exp(X_H) - 1.
$$

The implemented terminal return laws are:

**Gaussian**

$$
X_H = \mu H - \frac{1}{2}\sigma^2 H + \sigma\sqrt{H}Z,
\qquad Z \sim N(0,1).
$$

**Student-t**

$$
X_H = \mu H - \frac{1}{2}\sigma^2 H
      + \sigma\sqrt{H}\epsilon_{\nu},
$$

where $\epsilon_{\nu}$ is scaled to unit variance when $\nu > 2$.

**Merton jump diffusion**

$$
X_H = \left(\mu - \frac{1}{2}\sigma^2 - \lambda k\right)H
      + \sigma\sqrt{H}Z
      + N_H\mu_J + \sqrt{N_H}\sigma_J Z_J,
$$

where $N_H \sim \mathrm{Poisson}(\lambda H)$ and
$k = \exp(\mu_J + \sigma_J^2/2) - 1$.

Define downside $d_H = \max(0,-R^{ETH}_H)$. The peg drop and executable-depth
haircut are

$$
g_H = \mathrm{clip}(g_0 + \beta_g d_H + \epsilon_g, 0, g_{max}),
$$

$$
h_H = \mathrm{clip}(h_0 + \beta_h d_H + \epsilon_h, 0, h_{max}).
$$

The collateral price is

$$
P_H = P_0(1 + R^{ETH}_H)(1 - g_H).
$$

This construction couples ETH downside, peg dislocation, and depth loss
contemporaneously. It does not yet model a lagged peg response.

## 3. Multi-Period Stress Paths

Gaussian and jump-diffusion paths use coherent increments. Student-t paths
use one chi-square variance mixture per path, shared across periods. Given
that mixture, increments are Gaussian. Their sum therefore preserves the
configured terminal Student-t marginal instead of becoming artificially
thin-tailed as the number of periods increases.

The idiosyncratic peg residual can follow a discrete Ornstein-Uhlenbeck
process:

$$
x_t = \phi x_{t-1} + \eta_t,
\qquad \phi = \exp(-\kappa\Delta t).
$$

Here $\kappa$ is the mean-reversion speed in inverse days. The innovation
standard deviation is calibrated so variance at the configured stress
horizon $H_d$ equals the terminal peg variance $\sigma_g^2$:

$$
\sigma_{\eta}
= \sigma_g
\sqrt{\frac{1-\exp(-2\kappa\Delta t)}
{1-\exp(-2\kappa H_d)}}.
$$

At $\kappa=0$, this reduces to random-walk scaling:

$$
\sigma_{\eta}
= \sigma_g\sqrt{\frac{T/H_d}{n}},
$$

where $T$ is the total window and $n$ is the number of periods. The depth
residual currently follows that random-walk convention. At each period,
the current cumulative ETH return, current peg level, and current depth
haircut determine price and liquidation capacity. They do not reset after a
liquidation.

## 4. Slippage And Depth

The analytic fallback curve maps USD notional $Q$ to collateral quantity
$q = Q/\sqrt{P}$ and average slippage

$$
s(Q) = \frac{q}{L+q}.
$$

One reference quote $(Q_{ref},s_{ref})$ implies

$$
L = \frac{Q_{ref}}{\sqrt{P}}\frac{1-s_{ref}}{s_{ref}}.
$$

Real-market snapshots instead store observed aggregator quote points. The
empirical curve is linear in notional below the first point and linear in
log notional between points. Slippage is constrained to be nondecreasing.
Beyond the largest quote it is held flat, so reported slippage there is only
a lower bound and should not be interpreted as an executable quote. The final
empirical slippage value is clipped to the interval $[0,0.995]$.

A depth haircut $h$ is applied as

$$
s_h(Q) = s_0\left(\frac{Q}{1-h}\right).
$$

For the analytic curve this is exactly equivalent to replacing $L$ with
$L(1-h)$. Executable proceeds are

$$
E(Q) = Q[1-s(Q)].
$$

## 5. Liquidator Participation

If a liquidator repays $R$ and receives collateral worth $R(1+b)$ before
execution costs, break-even requires

$$
R(1+b)(1-s) \ge R.
$$

Therefore the maximum participating slippage is

$$
s \le \frac{b}{1+b}.
$$

This threshold is used by both the stochastic liquidation engine and the
deterministic clearance test.

## 6. V3 And V4 Liquidation Sizing

Under the V3 baseline, the close fraction is

$$
f_i =
\begin{cases}
1, & HF_i < HF_{full},\\
f_{close}, & HF_{full} \le HF_i < 1.
\end{cases}
$$

The repay and seize values are

$$
R_i = \min\left(f_iD_i,\frac{C_i}{1+b}\right),
\qquad Q_i = R_i(1+b).
$$

Under the V4 counterfactual, the bonus increases linearly between the
documented near-par and maximum-bonus anchors:

$$
b_i = b_{max}\left[f_b + (1-f_b)
\mathrm{clip}\left(\frac{1-HF_i}{1-HF_{max}},0,1\right)\right].
$$

Linear interpolation between those anchors is a model assumption.

V4 sizes repayment to a target health factor $T$ by solving

$$
[C_i-R_i(1+b_i)]LT_i = T(D_i-R_i).
$$

When the denominator is positive, the solution is

$$
R_i^{target}
= \frac{TD_i-C_iLT_i}{T-(1+b_i)LT_i}.
$$

The implemented repayment is

$$
R_i = \min\left(
\max(R_i^{target},f_{floor}D_i),
D_i,
\frac{C_i}{1+b_i}
\right).
$$

If a partial repayment would leave debt below the dust threshold, the engine
attempts a full close, still capped by seizable collateral. If the target
equation has a nonpositive denominator, it also attempts a full close.

## 7. Queue Clearing

The dashboard uses ordered clearing. In each scenario, liquidatable accounts
are sorted by bonus descending, with seize value descending as the tie-break.
Suppose cleared notional $V$ has already consumed depth. The next tranche
$Q_i$ is assigned marginal slippage

$$
s_i^{marginal}
= 1-\frac{E(V+Q_i)-E(V)}{Q_i}.
$$

It clears if $s_i^{marginal} \le b_i/(1+b_i)$. A cleared tranche increases
$V$; a stalled tranche does not. This represents competitive liquidators
selecting the most profitable available work first.

The aggregate research benchmark applies one average slippage to the whole
queued notional, then compares that value with each account's bonus. It is
computationally simpler but can overstate loss when early tranches would
clear before later tranches exhaust depth.

## 8. Bad-Debt Accounting

For a liquidation that clears, execution slippage is borne by the liquidator.
Protocol bad debt is only the insolvency gap:

$$
L_i^{clear} = \max\left(0,D_i-\frac{C_i}{1+b_i}\right).
$$

For a stalled liquidation, collateral is marked to delayed executable value:

$$
L_i^{stall}
= \max[0,D_i-C_i(1-s_i)(1-\delta)],
$$

where $\delta$ is the additional liquidation-delay drawdown. Scenario loss
is the sum across accounts.

This distinction prevents ordinary liquidator slippage from being counted as
protocol loss while incentives still work.

## 9. Multi-Period Book Evolution

Each period applies ordered clearing to the current book state.

- A solvent clear reduces stable and ETH debt pro rata and removes seized
  collateral units.
- An insolvent clear realizes the insolvency gap and closes the account.
- A stalled position remains open and can be reconsidered in later periods.
- At the terminal period, a position that is still underwater and stalled is
  marked using the delayed executable-value convention.

Let $V_t$ be depth already consumed before period $t$, $Q_t^{clear}$ the
period's cleared sale, and $\rho$ the replenishment fraction. The next offset
is

$$
V_{t+1} = (V_t + Q_t^{clear})(1-\rho).
$$

Thus $\rho=1$ restores the depth curve before the next period, while
$\rho=0$ carries all consumed depth forward. Replenishment resets only the
depth consumed by prior transactions. It does not reset price, peg, or the
scenario's depth haircut.

The single-shock benchmark and multi-period simulator use exactly matched
terminal scenarios. Their difference is therefore liquidation timing and
book-state evolution, not a different terminal return draw.

## 10. Risk Measures And Sparse Losses

For scenario loss $L \ge 0$:

$$
p_{loss}=P(L>0),
\qquad EL=E[L],
\qquad S=E[L\mid L>0].
$$

When positive losses exist,

$$
EL=p_{loss}S.
$$

For confidence level $\alpha$, empirical CVaR is the mean of the worst
$\lceil(1-\alpha)N\rceil$ draws. If $p_{loss}<1-\alpha$, this tail includes
zeros. In that sparse regime,

$$
CVaR_{\alpha}=\frac{EL}{1-\alpha}
$$

up to the finite-sample tail-count convention. CVaR can then be a useful
budget statistic, but it is not the severity of an outcome that can occur.
The reports therefore show positive-draw count, $p_{loss}$ with a Wilson
interval, expected loss, conditional severity, VaR, and CVaR. They warn when
fewer than 30 positive-loss draws support tail severity.

Cap sizing evaluates a grid of proportionally scaled books and selects the
largest exposure whose CVaR stays within the configured budget. Linear
interpolation is used between the final passing and first failing grid point.

## 11. Deterministic Clearance Test

For each eligible account, the conservative full-liquidation sale is

$$
Q_i = \min[C_i^{target},D_i(1+b)].
$$

The test selects the largest $Q_i$ and asks whether its slippage is within
$b/(1+b)$. With the analytic curve, maximum clearable notional is

$$
Q_{max}=\sqrt{P}Lb.
$$

With empirical depth, the monotone quote curve is inverted at the same
break-even threshold. Under haircut $h$, clearable notional scales by
$1-h$.

This is a strict instant routed-liquidity test. It excludes CEX, OTC, and
primary redemption capacity. For redeemable collateral, FAIL identifies a
time-horizon question rather than proving that eventual recovery is
insufficient.

## 12. V4 Hub Allocation

The Hub module is a synthetic allocation experiment. It is not yet connected
to the real-market borrower snapshots. For Spoke $k$, one systemic factor $Z$
and one idiosyncratic factor $e_k$ generate

$$
u_k=\rho_k Z+\sqrt{1-\rho_k^2}e_k.
$$

Hub loss is the scenario-wise sum of Spoke losses. Starting from zero credit,
the allocator tests one credit increment for every Spoke and assigns the
increment that produces the lowest resulting Hub CVaR. It stops when the Hub
balance or CVaR budget binds. The reported marginal risk measure is

$$
m_k=\frac{CVaR(L_{Hub}+\Delta L_k)-CVaR(L_{Hub})}{\Delta Credit_k}.
$$

The greedy discrete procedure approximately, not exactly, equalizes marginal
risk across funded Spokes.

## 13. Implementation Map

| Model component | Implementation |
|---|---|
| Return, peg, and depth scenarios | `stress.py` |
| Borrower state and exposure scaling | `positions.py`, `data/book.py` |
| Slippage curves | `slippage.py` |
| V3, V4, ordered queue, and bad debt | `liquidation.py` |
| Single-period metrics and cap sizing | `engine.py` |
| Largest-borrower clearance | `data/clearance.py` |
| Multi-period state evolution | `multiperiod.py` |
| Historical episode replay | `data/episodes.py` |
| Synthetic Hub allocation | `hub.py` |

Economic invariants and regression cases are in `tests/test_engine.py`,
`tests/test_data.py`, `tests/test_multiperiod.py`, and `tests/test_hub.py`.

## 14. Current Model Boundaries

The current implementation does not provide:

- coverage for debt positions, if any, that did not emit a `Borrow` event
  through the configured Pool proxy history, or accounts below the snapshot's
  current-debt floor;
- borrower-level correlated shocks across every collateral and debt asset;
- CEX, OTC, redemption-queue, or time-to-exit capacity;
- importance sampling for rare losses;
- archive reconstruction of historical borrower books;
- lagged coupling between ETH returns and peg dislocation;
- fixed-maturity PT discount, oracle, and AMM convergence dynamics;
- real V4 Hub risk-premium estimation.

These omissions are reported as modeling boundaries, not absorbed into hidden
calibration adjustments.
