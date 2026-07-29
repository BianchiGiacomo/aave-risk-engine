"""Snapshot schema and JSON persistence for market data.

Snapshots are committed to the repository so demos, tests, and CI run
offline and deterministically; live fetching only refreshes them.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field


@dataclass
class ReserveState:
    """One Aave V3 reserve: risk parameters, caps, and current usage."""

    symbol: str
    address: str
    decimals: int
    price_usd: float
    ltv: float
    liquidation_threshold: float
    liquidation_bonus: float
    reserve_factor: float
    borrow_cap_tokens: float
    supply_cap_tokens: float
    total_supplied_tokens: float
    total_debt_tokens: float

    @property
    def supplied_usd(self) -> float:
        return self.total_supplied_tokens * self.price_usd

    @property
    def supply_cap_usd(self) -> float:
        return self.supply_cap_tokens * self.price_usd


@dataclass
class AccountRecord:
    """One borrower account, aggregated in USD by Pool.getUserAccountData.

    `eth_debt_usd` is the USD value of the account's WETH-denominated
    variable debt. Debt in the collateral's own numeraire (a wstETH looper
    borrowing WETH) does not behave like USD-stable debt in a crash, because
    the debt leg falls with the collateral, so USD-shock books exclude
    ETH-debt-dominated accounts.
    """

    address: str
    collateral_usd: float
    debt_usd: float
    avg_liquidation_threshold: float
    health_factor: float
    target_collateral_usd: float
    eth_debt_usd: float = 0.0

    @property
    def target_share(self) -> float:
        return self.target_collateral_usd / self.collateral_usd if self.collateral_usd > 0 else 0.0

    @property
    def eth_debt_share(self) -> float:
        return self.eth_debt_usd / self.debt_usd if self.debt_usd > 0 else 0.0


@dataclass
class DepthCalibration:
    """Slippage-curve calibration fitted from real sell quotes."""

    source: str
    pair: str
    quoted_at: int
    points: list[list[float]]  # [notional_usd, fractional_slippage]
    liquidity: float
    ref_notional_usd: float
    ref_slippage: float


@dataclass
class StressCalibration:
    """Return-law and peg parameters estimated from price history."""

    source: str
    lookback_days: int
    annual_vol: float
    t_dof: float | None = None
    peg_pass: bool | None = None
    peg_worst_deviation: float | None = None
    peg_max_run_days: float | None = None
    peg_daily_vol: float | None = None


@dataclass
class MarketSnapshot:
    """A dated, single-market view of everything the engine needs.

    `scan_blocks` is the Borrow-event discovery window; borrowers dormant
    for longer than that are not sampled, so it belongs in any presentation
    of the account data.
    """

    chain: str
    block: int
    timestamp: int
    reserve: ReserveState
    accounts: list[AccountRecord] = field(default_factory=list)
    depth: DepthCalibration | None = None
    stress: StressCalibration | None = None
    notes: str = ""
    scan_blocks: int | None = None


def default_snapshot_path(symbol: str = "wstETH", chain: str = "ethereum") -> str:
    name = f"aave_v3_{chain}_{symbol.lower()}.json"
    return os.path.join(os.path.dirname(__file__), "snapshots", name)


def save_snapshot(snapshot: MarketSnapshot, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(asdict(snapshot), fh, indent=1)


def load_snapshot(path: str | None = None) -> MarketSnapshot:
    with open(path or default_snapshot_path(), encoding="utf-8") as fh:
        raw = json.load(fh)
    return MarketSnapshot(
        chain=raw["chain"],
        block=raw["block"],
        timestamp=raw["timestamp"],
        reserve=ReserveState(**raw["reserve"]),
        accounts=[AccountRecord(**a) for a in raw["accounts"]],
        depth=DepthCalibration(**raw["depth"]) if raw.get("depth") else None,
        stress=StressCalibration(**raw["stress"]) if raw.get("stress") else None,
        notes=raw.get("notes", ""),
        scan_blocks=raw.get("scan_blocks"),
    )
