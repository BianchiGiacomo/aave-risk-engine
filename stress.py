"""Correlated stress scenarios and pluggable return laws."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import AssetParams, LiquidityParams, StressConfig
from .slippage import calibrate_liquidity


@dataclass
class Scenarios:
    """Sampled scenario inputs."""

    coll_price: np.ndarray
    depth_liquidity: np.ndarray
    eth_return: np.ndarray
    peg_drop: np.ndarray
    depth_haircut: np.ndarray


def _standardized_t(dof: float, size, rng: np.random.Generator) -> np.ndarray:
    t = rng.standard_t(dof, size=size)
    if dof > 2:
        t *= np.sqrt((dof - 2.0) / dof)
    return t


def sample_log_returns(
    stress: StressConfig,
    h: float,
    n: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Draw terminal H-year log returns under the configured return law."""
    model = stress.return_model
    mu = stress.eth_annual_drift
    sigma = stress.eth_annual_vol * np.sqrt(h)

    if model == "gaussian":
        z = rng.standard_normal(n)
        return mu * h - 0.5 * sigma**2 + sigma * z

    if model == "student_t":
        eps = _standardized_t(stress.tail_dof, n, rng)
        return mu * h - 0.5 * sigma**2 + sigma * eps

    if model == "jump_diffusion":
        lam = stress.jump_intensity
        jm = stress.jump_mean
        jv = stress.jump_vol
        kappa = np.exp(jm + 0.5 * jv**2) - 1.0
        drift = (mu - 0.5 * stress.eth_annual_vol**2 - lam * kappa) * h
        z = rng.standard_normal(n)
        n_jumps = rng.poisson(lam * h, n)
        jump = n_jumps * jm + np.sqrt(n_jumps) * jv * rng.standard_normal(n)
        return drift + sigma * z + jump

    raise ValueError(f"unknown return_model: {model!r}")


def sample_scenarios(
    stress: StressConfig,
    asset: AssetParams,
    liq: LiquidityParams,
    n: int,
    rng: np.random.Generator,
    log_return: np.ndarray | None = None,
) -> Scenarios:
    """Draw correlated collateral, peg, and liquidity-depth scenarios."""
    h = stress.horizon_days / 365.0
    log_ret = sample_log_returns(stress, h, n, rng) if log_return is None else log_return
    if log_ret.shape[0] != n:
        raise ValueError("log_return must have length n")

    eth_ret = np.exp(log_ret) - 1.0
    downside = np.maximum(0.0, -eth_ret)

    peg_drop = (
        stress.base_peg_drop
        + stress.peg_crash_beta * downside
        + stress.peg_idio_vol * rng.standard_normal(n)
    )
    peg_drop = np.clip(peg_drop, 0.0, stress.max_peg_drop)

    coll_price = asset.spot_price * (1.0 + eth_ret) * (1.0 - peg_drop)
    coll_price = np.maximum(coll_price, 1e-9)

    haircut = (
        stress.base_depth_haircut
        + stress.depth_crash_beta * downside
        + stress.depth_idio_vol * rng.standard_normal(n)
    )
    haircut = np.clip(haircut, 0.0, stress.max_depth_haircut)

    l0 = calibrate_liquidity(asset.spot_price, liq.ref_notional_usd, liq.ref_slippage)
    depth_liquidity = l0 * (1.0 - haircut)

    return Scenarios(
        coll_price=coll_price,
        depth_liquidity=depth_liquidity,
        eth_return=eth_ret,
        peg_drop=peg_drop,
        depth_haircut=haircut,
    )
