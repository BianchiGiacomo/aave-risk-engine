"""Run the multi-Spoke Hub allocation demo."""

from __future__ import annotations

from dataclasses import replace

from .config import LiquidityParams, RiskParams, ScenarioConfig, SimConfig
from .hub import Hub, Spoke


def _fmt(x: float) -> str:
    for unit, div in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(x) >= div:
            return f"${x / div:,.1f}{unit}"
    return f"${x:,.0f}"


def _spoke_config(name, spot, vol, lt, ltv, bonus, depth, peg_beta, depth_beta):
    base = ScenarioConfig()
    return replace(
        base,
        asset=replace(base.asset, name=name, spot_price=spot),
        risk=RiskParams(ltv=ltv, liquidation_threshold=lt, liquidation_bonus=bonus),
        liquidity=LiquidityParams(ref_notional_usd=depth, ref_slippage=0.02),
        stress=replace(
            base.stress,
            eth_annual_vol=vol,
            peg_crash_beta=peg_beta,
            base_peg_drop=0.001 if peg_beta > 0 else 0.0,
            depth_crash_beta=depth_beta,
        ),
    )


def main() -> None:
    spokes = [
        Spoke("stETH", _spoke_config("stETH", 3_000, 0.80, 0.86, 0.82, 0.05, 25e6, 0.25, 1.3), 0.95),
        Spoke("WBTC", _spoke_config("WBTC", 60_000, 0.72, 0.83, 0.79, 0.06, 45e6, 0.0, 1.0), 0.85),
        Spoke("LONGTAIL", _spoke_config("LONGTAIL", 5, 1.05, 0.72, 0.66, 0.09, 6e6, 0.0, 1.6), 0.45),
    ]
    market = replace(ScenarioConfig().stress, return_model="jump_diffusion")
    hub_balance = 700e6
    budget = 8e6
    hub = Hub(
        spokes,
        market,
        hub_balance=hub_balance,
        sim=SimConfig(n_scenarios=10_000, seed=7, chunk_size=2_000),
        severity=1.0,
    )
    res = hub.allocate(budget_usd=budget, increment_usd=hub_balance / 40.0)

    print("Aave V4 Hub-Spoke credit-line allocation")
    print(f"Hub balance: {_fmt(hub_balance)} | budget: {_fmt(budget)}")
    print(f"{'Spoke':<10}{'rho':>6}{'credit line':>16}{'standalone CVaR':>18}{'risk premium /$1m':>20}")
    for sp in spokes:
        name = sp.name
        print(
            f"{name:<10}{sp.rho:>6.2f}{_fmt(res.allocation[name]):>16}"
            f"{_fmt(res.standalone_cvar[name]):>18}"
            f"{_fmt(res.marginal_cvar[name] * 1e6):>20}"
        )
    print(f"\ntotal credit: {_fmt(res.total_allocated)} ({res.total_allocated / hub_balance:.0%} of balance)")
    print(f"Hub CVaR99: {_fmt(res.hub_cvar)}")
    print(f"sum standalone CVaR: {_fmt(sum(res.standalone_cvar.values()))}")
    print(f"diversification gain: {_fmt(res.diversification_benefit)}")


if __name__ == "__main__":
    main()
