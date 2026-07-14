"""Economics tests for the multi-Spoke Hub allocator."""

from __future__ import annotations

from dataclasses import replace

from aave_risk_engine.config import LiquidityParams, ScenarioConfig, SimConfig
from aave_risk_engine.hub import Hub, Spoke


SIM = SimConfig(n_scenarios=12_000, seed=3, chunk_size=4_000)


def _base() -> ScenarioConfig:
    return ScenarioConfig(sim=SIM)


def test_diversification_benefit_nonnegative():
    base = _base()
    hub = Hub([Spoke("a", base, 0.5), Spoke("b", base, 0.5)], base.stress, 300e6, SIM)
    res = hub.evaluate({"a": 150e6, "b": 150e6})
    assert res.diversification_benefit >= -1e-6


def test_correlation_increases_hub_tail():
    base = _base()
    alloc = {"a": 150e6, "b": 150e6}
    corr = Hub([Spoke("a", base, 1.0), Spoke("b", base, 1.0)], base.stress, 300e6, SIM).evaluate(alloc)
    ind = Hub([Spoke("a", base, 0.0), Spoke("b", base, 0.0)], base.stress, 300e6, SIM).evaluate(alloc)
    assert corr.hub_cvar > ind.hub_cvar
    assert ind.diversification_benefit > 0


def test_allocates_more_to_deeper_liquidity():
    base = _base()
    deep = replace(base, liquidity=LiquidityParams(ref_notional_usd=60e6, ref_slippage=0.02))
    thin = replace(base, liquidity=LiquidityParams(ref_notional_usd=10e6, ref_slippage=0.02))
    hub = Hub([Spoke("deep", deep, 0.9), Spoke("thin", thin, 0.9)], base.stress, 240e6, SIM)
    res = hub.allocate(1e12, increment_usd=20e6)
    assert res.allocation["deep"] >= res.allocation["thin"]


def test_budget_binds():
    base = _base()
    hub = Hub([Spoke("a", base, 0.9), Spoke("b", base, 0.9)], base.stress, 400e6, SIM)
    tight = hub.allocate(2e6, increment_usd=20e6)
    loose = hub.allocate(1e12, increment_usd=20e6)
    assert tight.total_allocated < loose.total_allocated
    assert loose.total_allocated <= 400e6 + 1.0


def test_severity_scales_hub_risk():
    base = _base()
    spokes = [Spoke("a", base, 0.9), Spoke("b", base, 0.8)]
    alloc = {"a": 150e6, "b": 150e6}
    low = Hub(spokes, base.stress, 300e6, SIM, severity=0.7).evaluate(alloc)
    high = Hub(spokes, base.stress, 300e6, SIM, severity=1.5).evaluate(alloc)
    assert high.hub_cvar > low.hub_cvar


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} hub tests passed.")


if __name__ == "__main__":
    _run_all()
