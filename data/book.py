"""Turn a MarketSnapshot into engine inputs: real books and calibrated configs.

Modeling convention ("effective single-asset account"): each account's total
collateral is treated as the target asset and shocked by one scenario price,
with the account's own on-chain weighted-average liquidation threshold. This
is the perfectly-correlated-collateral view; filtering by `min_target_share`
keeps it honest by restricting to accounts actually dominated by the target.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from ..config import LiquidityParams, RiskParams, ScenarioConfig
from ..positions import PositionBook, scale_book
from .snapshot import MarketSnapshot

__all__ = ["build_real_book", "scale_book", "scenario_config_from_snapshot"]


def build_real_book(
    snapshot: MarketSnapshot,
    min_target_share: float = 0.5,
    min_debt_usd: float = 10_000.0,
    max_accounts: int | None = None,
    max_eth_debt_share: float = 0.5,
    model_eth_debt: bool = False,
) -> PositionBook:
    """Build a PositionBook from real accounts dominated by the target asset.

    Two treatments of WETH-denominated debt are supported:

    - default (`model_eth_debt=False`): accounts whose debt is mostly
      WETH-denominated are excluded, because their debt leg falls with
      ETH-correlated collateral in a USD crash and a stable-debt shock
      would mismodel them;
    - `model_eth_debt=True`: all target-dominant accounts are included and
      each position's ETH-denominated debt portion is carried on the book,
      so the engine scales it with the scenario ETH return. Loopers are
      then stressed by the peg/exchange-rate and depth terms, which is
      their true risk vector.
    """
    selected = [
        a
        for a in snapshot.accounts
        if a.debt_usd >= min_debt_usd
        and a.collateral_usd > 0
        and a.target_share >= min_target_share
        and (model_eth_debt or a.eth_debt_share <= max_eth_debt_share)
        and 0 < a.avg_liquidation_threshold < 1
    ]
    selected.sort(key=lambda a: a.debt_usd, reverse=True)
    if max_accounts is not None:
        selected = selected[:max_accounts]
    if not selected:
        raise ValueError("no accounts pass the filters; loosen min_target_share/min_debt_usd")

    price = snapshot.reserve.price_usd
    debt = np.array([a.debt_usd for a in selected])
    coll_units = np.array([a.collateral_usd / price for a in selected])
    lt = np.array([a.avg_liquidation_threshold for a in selected])
    hf0 = coll_units * price * lt / debt
    eth_debt = None
    if model_eth_debt:
        eth_debt = np.array([min(max(a.eth_debt_usd, 0.0), a.debt_usd) for a in selected])
    return PositionBook(
        debt_usd=debt, coll_units=coll_units, hf0=hf0, lt=lt, eth_debt_usd=eth_debt
    )


def scenario_config_from_snapshot(
    snapshot: MarketSnapshot,
    base: ScenarioConfig | None = None,
) -> ScenarioConfig:
    """Calibrate a ScenarioConfig from snapshot data, keeping other defaults.

    Wires in: oracle spot price, on-chain LTV/LT/bonus, fitted depth curve,
    realized volatility, and fitted Student-t tail (when heavy tails were
    detected; otherwise the base return law is kept).
    """
    cfg = base or ScenarioConfig()
    reserve = snapshot.reserve

    asset = replace(cfg.asset, name=reserve.symbol, spot_price=reserve.price_usd)
    risk = RiskParams(
        ltv=reserve.ltv,
        liquidation_threshold=reserve.liquidation_threshold,
        liquidation_bonus=reserve.liquidation_bonus,
        close_factor=cfg.risk.close_factor,
        full_liquidation_hf=cfg.risk.full_liquidation_hf,
    )

    liquidity = cfg.liquidity
    if snapshot.depth is not None:
        points = snapshot.depth.points
        liquidity = LiquidityParams(
            ref_notional_usd=snapshot.depth.ref_notional_usd,
            ref_slippage=snapshot.depth.ref_slippage,
            # Interpolating needs at least two quotes; otherwise fall back
            # to the analytic curve through the reference point.
            depth_points=points if points and len(points) >= 2 else None,
        )

    stress = cfg.stress
    if snapshot.stress is not None:
        stress = replace(stress, eth_annual_vol=snapshot.stress.annual_vol)
        if snapshot.stress.t_dof is not None:
            stress = replace(stress, tail_dof=snapshot.stress.t_dof)
        if snapshot.stress.peg_pass is None:
            # No peg series was calibrated: the asset is its own underlying
            # (WETH, WBTC), so peg stress does not apply to it.
            stress = replace(
                stress, base_peg_drop=0.0, peg_crash_beta=0.0, peg_idio_vol=0.0
            )
        elif snapshot.stress.peg_daily_vol is not None:
            # Idiosyncratic peg noise over the stress horizon, from the
            # observed LST/underlying ratio history. The crash-coupled peg
            # terms (base and beta) stay at their configured values.
            stress = replace(
                stress,
                peg_idio_vol=snapshot.stress.peg_daily_vol
                * float(np.sqrt(stress.horizon_days)),
            )
        if snapshot.stress.peg_mean_reversion_speed is not None:
            stress = replace(
                stress,
                peg_mean_reversion_speed=snapshot.stress.peg_mean_reversion_speed,
            )

    return replace(cfg, asset=asset, risk=risk, liquidity=liquidity, stress=stress)
