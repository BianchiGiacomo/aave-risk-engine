"""Build a MarketSnapshot from live public data sources.

Usage:
    python -m aave_risk_engine.data.build_snapshot [--asset wstETH] [--blocks 100000]

Sources (all keyless): public Ethereum JSON-RPC endpoints for Aave V3 state
and borrower discovery, Paraswap for routed sell quotes, Kraken for USD
price history, Coingecko for LST/underlying peg history.
"""

from __future__ import annotations

import argparse
import json
import os
import time

from . import aave_v3, depth, markets
from .rpc import EthRpc
from .snapshot import (
    AccountRecord,
    MarketSnapshot,
    ReserveState,
    StressCalibration,
    default_snapshot_path,
    save_snapshot,
)


def build_reserve_state(rpc: EthRpc, symbol: str, asset: str) -> ReserveState:
    data_provider, oracle = aave_v3.resolve_contracts(rpc)
    config = aave_v3.reserve_configuration(rpc, data_provider, asset)
    caps = aave_v3.reserve_caps(rpc, data_provider, asset)
    usage = aave_v3.reserve_usage(rpc, data_provider, asset, config["decimals"])
    price = aave_v3.asset_price_usd(rpc, oracle, asset)
    return ReserveState(
        symbol=symbol,
        address=asset,
        decimals=config["decimals"],
        price_usd=price,
        ltv=config["ltv"],
        liquidation_threshold=config["liquidation_threshold"],
        liquidation_bonus=config["liquidation_bonus"],
        reserve_factor=config["reserve_factor"],
        borrow_cap_tokens=caps["borrow_cap_tokens"],
        supply_cap_tokens=caps["supply_cap_tokens"],
        total_supplied_tokens=usage["total_supplied_tokens"],
        total_debt_tokens=usage["total_debt_tokens"],
    )


def _load_borrower_cache(path: str, head: int, blocks: int, max_age_blocks: int = 3_000) -> list[str] | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        cache = json.load(fh)
    fresh = head - cache.get("head", 0) <= max_age_blocks
    wide_enough = cache.get("blocks", 0) >= blocks
    return cache["users"] if fresh and wide_enough else None


def build_accounts(
    rpc: EthRpc,
    reserve: ReserveState,
    blocks: int,
    chunk_blocks: int,
    min_debt_usd: float,
    top_n: int,
    cache_path: str | None = None,
) -> list[AccountRecord]:
    data_provider, oracle = aave_v3.resolve_contracts(rpc)
    atoken = aave_v3.atoken_address(rpc, data_provider, reserve.address)
    weth = aave_v3.TOKENS["WETH"]
    weth_debt_token = aave_v3.variable_debt_token_address(rpc, data_provider, weth)
    weth_price = aave_v3.asset_price_usd(rpc, oracle, weth)
    head = rpc.block_number()

    users = _load_borrower_cache(cache_path, head, blocks) if cache_path else None
    if users is not None:
        print(f"reusing {len(users)} cached borrowers ({cache_path})")
    else:
        print(f"scanning Borrow events over {blocks:,} blocks ...")
        users = sorted(aave_v3.discover_borrowers(rpc, head - blocks, head, chunk_blocks))
        print(f"  {len(users)} unique borrowers")
        if cache_path:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump({"head": head, "blocks": blocks, "users": users}, fh)

    print("fetching account data ...")
    accounts = [a for a in aave_v3.account_data(rpc, users) if a["debt_usd"] >= min_debt_usd]
    accounts.sort(key=lambda a: a["debt_usd"], reverse=True)
    accounts = accounts[:top_n]
    print(f"  {len(accounts)} accounts with debt >= ${min_debt_usd:,.0f}")

    print(f"fetching {reserve.symbol} collateral and WETH debt balances ...")
    addresses = [a["address"] for a in accounts]
    balances = aave_v3.token_balances(rpc, atoken, addresses, reserve.decimals)
    weth_debts = aave_v3.token_balances(rpc, weth_debt_token, addresses, 18)
    return [
        AccountRecord(
            address=a["address"],
            collateral_usd=a["collateral_usd"],
            debt_usd=a["debt_usd"],
            avg_liquidation_threshold=a["avg_liquidation_threshold"],
            health_factor=a["health_factor"],
            target_collateral_usd=balances[a["address"]] * reserve.price_usd,
            eth_debt_usd=weth_debts[a["address"]] * weth_price,
        )
        for a in accounts
    ]


def build_stress_calibration(
    kraken_pair: str, peg_coin: str | None, lookback_days: int
) -> StressCalibration:
    closes = markets.kraken_daily_closes(kraken_pair)
    calibration = StressCalibration(
        source=f"kraken:{kraken_pair}" + (f"+coingecko:{peg_coin}" if peg_coin else ""),
        lookback_days=lookback_days,
        annual_vol=markets.realized_annual_vol(closes, lookback_days),
        t_dof=markets.fit_student_t_dof(closes, lookback_days),
    )
    if peg_coin:
        ratios = markets.coingecko_ratio_history(peg_coin, "eth", lookback_days)
        peg = markets.arfc_peg_check(ratios)
        calibration.peg_pass = peg["peg_pass"]
        calibration.peg_worst_deviation = peg["peg_worst_deviation"]
        calibration.peg_max_run_days = peg["peg_max_run_days"]
    return calibration


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", default="wstETH", choices=sorted(aave_v3.TOKENS))
    parser.add_argument("--pair-dest", default="WETH", choices=sorted(aave_v3.TOKENS))
    parser.add_argument("--blocks", type=int, default=100_000, help="borrower scan window")
    parser.add_argument("--chunk-blocks", type=int, default=5_000)
    parser.add_argument("--top", type=int, default=400, help="accounts kept, by debt")
    parser.add_argument("--min-debt", type=float, default=10_000.0)
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--kraken-pair", default="ETHUSD")
    parser.add_argument("--peg-coin", default="staked-ether", help="'' to skip the peg check")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    rpc = EthRpc()
    address = aave_v3.TOKENS[args.asset]

    print(f"reading {args.asset} reserve state ...")
    reserve = build_reserve_state(rpc, args.asset, address)
    print(
        f"  price ${reserve.price_usd:,.2f} | LT {reserve.liquidation_threshold:.2%} "
        f"| bonus {reserve.liquidation_bonus:.2%} | supplied {reserve.total_supplied_tokens:,.0f}"
    )

    cache_path = os.path.join(os.path.dirname(__file__), "snapshots", ".borrowers_cache.json")
    accounts = build_accounts(
        rpc, reserve, args.blocks, args.chunk_blocks, args.min_debt, args.top,
        cache_path=cache_path,
    )

    print("calibrating stress from price history ...")
    stress = None
    try:
        stress = build_stress_calibration(args.kraken_pair, args.peg_coin or None, args.lookback_days)
        print(f"  vol {stress.annual_vol:.1%} | t-dof {stress.t_dof} | peg pass {stress.peg_pass}")
    except Exception as exc:  # noqa: BLE001 - degrade to model defaults
        print(f"  WARNING: stress calibration failed ({exc}); snapshot will carry none")

    print("quoting sell ladder for depth calibration ...")
    fitted = None
    try:
        quotes = depth.paraswap_sell_quotes(address, aave_v3.TOKENS[args.pair_dest])
        points = depth.quotes_to_slippage_points(quotes, reserve.price_usd)
        fitted = depth.fit_depth(
            points, reserve.price_usd, "paraswap", f"{args.asset}/{args.pair_dest}"
        )
        print(f"  ref {fitted.ref_notional_usd / 1e6:,.1f}m @ {fitted.ref_slippage:.3%}")
    except Exception as exc:  # noqa: BLE001 - degrade to model defaults
        print(f"  WARNING: depth calibration failed ({exc}); snapshot will carry none")

    snapshot = MarketSnapshot(
        chain="ethereum",
        block=rpc.block_number(),
        timestamp=int(time.time()),
        reserve=reserve,
        accounts=accounts,
        depth=fitted,
        stress=stress,
        notes=(
            f"Borrowers discovered from Borrow events over the last {args.blocks:,} blocks; "
            "dormant borrowers outside that window are not sampled."
        ),
        scan_blocks=args.blocks,
    )
    path = args.out or default_snapshot_path(args.asset)
    save_snapshot(snapshot, path)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
