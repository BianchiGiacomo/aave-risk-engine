## Update: From Instant Depth To Economic Liquidation Capacity

The original note ended with a question: should clearance for redeemable
collateral be judged only against instant routed depth, or against capacity
over a stated horizon? I extended the wstETH case from a capacity test into a
liquidator balance-sheet sensitivity.

The strict result is unchanged. A `$256.52m` wstETH sale remains far above the
`$2.73m` that current routed DEX quotes clear instantly within the 6% bonus.
That test is still `FAIL` and excludes redemption, CEX, and OTC liquidity.

The extension asks a different question. Suppose a liquidator repays about
`$242m` at time zero, receives the collateral, hedges ETH/USD, and warehouses
the position while exiting through DEX liquidity or primary redemption. Can
the liquidation bonus cover recovery losses, funding, hedge carry, and the
required return on capital?

The route optimizer selects one total DEX/redemption allocation that maximizes
economic profit, then executes each allocation at its earliest modeled
capacity. The published sensitivity uses:

- 10% annual funding and a separate 10% annual capital hurdle;
- 0.10% hedge entry and 2% annual hedge carry;
- a 1% DEX execution-loss ceiling;
- a 24-hour redemption delay and illustrative `$25m/day` throughput;
- 4% canonical or oracle-to-recovery loss and zero additional DEX discount.

```text
route                         clear time   economic profit   minimum bonus
quiet DEX only                   30.11d          -$0.66m            6.29%
stressed DEX only               241.88d         -$16.92m           13.49%
optimized DEX + redemption       11.26d           $3.11m            4.66%
```

At the assumed redemption rate, the optimizer assigns all `$256.52m` to
redemption. Quiet and stressed blended rows therefore coincide: DEX depth is
not decision-relevant once the chosen route avoids it. This is not evidence
that a 6% bonus is sufficient in practice. It shows that the conclusion is
controlled by the redemption assumption.

That dependence is visible in a correlated stress sensitivity. Halving
stressed redemption throughput to `$12.5m/day` increases exit time to `21.08d`,
reduces economic profit to `$2.37m`, and raises the minimum bonus to `4.98%`.
The optimizer then assigns `$5.49m` to DEX and `$251.03m` to redemption.

![wstETH liquidator warehouse sensitivity](https://raw.githubusercontent.com/BianchiGiacomo/aave-risk-engine/v0.2.0/docs/assets/wsteth_liquidator_balance_sheet.png)

The main limitation is now explicit: `$25m/day` is a sensitivity input, not a
measured Lido stress-throughput estimate. The model also assumes sufficient
full-upfront financing, deterministic future capacity, and a static route
split. It does not establish that redemption slots or the required hedge can
be secured. These outputs are not a wstETH safety claim or a liquidation-bonus
recommendation.

This suggests that two tests may be more informative than forcing one
interpretation:

1. strict instant clearance against observable routed depth;
2. horizon-adjusted economic clearance under explicit funding, canonical-loss,
   and redemption-throughput stresses.

A governance conclusion should not treat the second test as a pass on an
uncalibrated throughput assumption. Its current value is to identify the
empirical quantity that controls the answer.

> For redeemable collateral, should the Risk Framework supplement instant
> clearance with a specified exit horizon and stressed primary-redemption
> throughput, while requiring the liquidation bonus to compensate the
> liquidator's capital and recovery risk over that horizon?

[Exact results and commands](https://github.com/BianchiGiacomo/aave-risk-engine/blob/v0.2.0/docs/results.md#7-mainnet-wsteth-liquidator-balance-sheet)
| [Run manifest](https://github.com/BianchiGiacomo/aave-risk-engine/blob/v0.2.0/docs/manifests/ethereum-wsteth-liquidator-balance-sheet-2026-08-18.json)
| [Model specification](https://github.com/BianchiGiacomo/aave-risk-engine/blob/v0.2.0/docs/model-specification.md)

```bash
python -m aave_risk_engine.run_liquidator_balance_sheet \
  --snapshot data/snapshots/aave_v3_ethereum_wsteth.json \
  --redemption-usd-per-day 25000000 \
  --canonical-losses 0.04
```
