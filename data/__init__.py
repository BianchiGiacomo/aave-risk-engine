"""Market-data layer: on-chain Aave V3 state, price history, and DEX depth.

Everything here degrades gracefully to committed JSON snapshots so the rest of
the package works offline and deterministically.
"""

from .book import build_real_book, scale_book
from .clearance import ClearanceResult, arfc_clearance_test, max_notional_at_slippage
from .snapshot import (
    AccountRecord,
    BorrowerDiscovery,
    DepthCalibration,
    MarketSnapshot,
    ReserveState,
    StressCalibration,
    default_snapshot_path,
    load_snapshot,
    save_snapshot,
    snapshot_from_json,
    snapshot_to_json,
)

__all__ = [
    "AccountRecord",
    "BorrowerDiscovery",
    "ClearanceResult",
    "DepthCalibration",
    "MarketSnapshot",
    "ReserveState",
    "StressCalibration",
    "arfc_clearance_test",
    "build_real_book",
    "default_snapshot_path",
    "load_snapshot",
    "max_notional_at_slippage",
    "save_snapshot",
    "scale_book",
    "snapshot_from_json",
    "snapshot_to_json",
]
