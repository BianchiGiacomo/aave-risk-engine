"""Configuration dataclasses for the Aave risk engine."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AssetParams:
    """Collateral asset and USD spot price."""

    name: str = "stETH"
    spot_price: float = 3_000.0
    debt_asset: str = "USDC"


@dataclass
class RiskParams:
    """Aave-style market risk parameters."""

    ltv: float = 0.80
    liquidation_threshold: float = 0.85
    liquidation_bonus: float = 0.05
    close_factor: float = 0.50
    full_liquidation_hf: float = 0.95

    def __post_init__(self) -> None:
        if not 0 < self.ltv <= self.liquidation_threshold < 1:
            raise ValueError("require 0 < ltv <= liquidation_threshold < 1")
        if not 0 < self.liquidation_bonus < 1:
            raise ValueError("require liquidation_bonus in (0, 1)")
        if not 0 < self.close_factor <= 1:
            raise ValueError("require close_factor in (0, 1]")
        if not 0 < self.full_liquidation_hf <= 1:
            raise ValueError("require full_liquidation_hf in (0, 1]")


@dataclass
class LiquidityParams:
    """Reference liquidation depth used to calibrate the slippage curve.

    When `depth_points` (observed [notional_usd, slippage] quotes) are
    present, the engine interpolates them directly instead of the analytic
    single-L curve; the reference point remains for calibrating scenario
    depth haircuts and for components that need a smooth curve.
    """

    ref_notional_usd: float = 25_000_000.0
    ref_slippage: float = 0.02
    depth_points: list[list[float]] | None = None


@dataclass
class StressConfig:
    """Joint stress model over a short liquidation horizon."""

    horizon_days: float = 2.0
    eth_annual_vol: float = 0.75
    eth_annual_drift: float = 0.0
    tail_dof: float = 3.0

    # Terminal H-day return law.
    return_model: str = "student_t"  # student_t | jump_diffusion | gaussian

    # Merton jump parameters used by return_model == "jump_diffusion".
    jump_intensity: float = 4.0
    jump_mean: float = -0.15
    jump_vol: float = 0.10

    # stETH / ETH peg.
    base_peg_drop: float = 0.001
    peg_crash_beta: float = 0.25
    peg_idio_vol: float = 0.004
    max_peg_drop: float = 0.30

    # Executable-depth haircut.
    base_depth_haircut: float = 0.10
    depth_crash_beta: float = 1.20
    depth_idio_vol: float = 0.05
    max_depth_haircut: float = 0.90

    # If liquidations stall (slippage > bonus / (1 + bonus)), collateral is
    # marked down further while the queue waits for liquidity.
    liquidation_delay_drawdown: float = 0.10


@dataclass
class PositionConfig:
    """Synthetic borrower book parameters."""

    total_debt_usd: float = 200_000_000.0
    n_borrowers: int = 1_500
    hf0_median: float = 1.6
    hf0_sigma: float = 0.45
    hf0_floor: float = 1.02
    debt_pareto_alpha: float = 1.5


@dataclass
class SimConfig:
    """Monte Carlo controls."""

    n_scenarios: int = 40_000
    seed: int = 7
    chunk_size: int = 2_000
    cvar_level: float = 0.99


@dataclass
class ScenarioConfig:
    """A fully specified single-Spoke run."""

    asset: AssetParams = field(default_factory=AssetParams)
    risk: RiskParams = field(default_factory=RiskParams)
    liquidity: LiquidityParams = field(default_factory=LiquidityParams)
    stress: StressConfig = field(default_factory=StressConfig)
    positions: PositionConfig = field(default_factory=PositionConfig)
    sim: SimConfig = field(default_factory=SimConfig)
