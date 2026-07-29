"""Build a MarketSnapshot from live public data sources.

Usage:
    python -m aave_risk_engine.data.build_snapshot [--chain ethereum] [--asset wstETH]

Sources (all keyless): public JSON-RPC endpoints for Aave V3 state and
borrower discovery, Paraswap or KyberSwap for routed sell quotes, Kraken
for USD price history, Coingecko for LST/underlying peg history.
"""

from __future__ import annotations

import argparse
import json
import os
import time

from . import aave_v3, depth, markets
from .aave_v3 import CHAINS, ChainConfig
from .rpc import EthRpc
from .snapshot import (
    AccountRecord,
    MarketSnapshot,
    ReserveState,
    StressCalibration,
    default_snapshot_path,
    save_snapshot,
)

# Per-asset calibration defaults: (kraken pair for the USD vol driver,
# coingecko id for the peg ratio or None when the asset is not pegged to a
# more liquid underlying).
ASSET_CALIBRATION = {
    "wstETH": ("ETHUSD", "staked-ether"),
    "weETH": ("ETHUSD", "wrapped-eeth"),
    "WETH": ("ETHUSD", None),
    "WBTC": ("XBTUSD", None),
    "USDC": ("USDCUSD", None),
    "USDT": ("USDTUSD", None),
}

# Quote-ladder sizes in USD; converted to token amounts at the oracle price.
DEFAULT_LADDER_USD = (25e3, 100e3, 500e3, 2e6, 8e6, 25e6)

# Default Borrow-event scan windows, sized to roughly two weeks of blocks
# on each chain (Ethereum ~12s blocks, Linea ~2s blocks).
DEFAULT_SCAN_BLOCKS = {"ethereum": 100_000, "linea": 600_000}


def build_reserve_state(rpc: EthRpc, chain: ChainConfig, symbol: str, asset: str) -> ReserveState:
    data_provider, oracle = aave_v3.resolve_contracts(rpc, chain)
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
    chain: ChainConfig,
    reserve: ReserveState,
    blocks: int,
    chunk_blocks: int,
    min_debt_usd: float,
    top_n: int,
    cache_path: str | None = None,
) -> list[AccountRecord]:
    data_provider, oracle = aave_v3.resolve_contracts(rpc, chain)
    atoken = aave_v3.atoken_address(rpc, data_provider, reserve.address)
    weth = chain.tokens["WETH"]
    weth_debt_token = aave_v3.variable_debt_token_address(rpc, data_provider, weth)
    weth_price = aave_v3.asset_price_usd(rpc, oracle, weth)
    head = rpc.block_number()

    users = _load_borrower_cache(cache_path, head, blocks) if cache_path else None
    if users is not None:
        print(f"reusing {len(users)} cached borrowers ({cache_path})")
    else:
        print(f"scanning Borrow events over {blocks:,} blocks ...")
        users = sorted(
            aave_v3.discover_borrowers(rpc, head - blocks, head, chunk_blocks, chain=chain)
        )
        print(f"  {len(users)} unique borrowers")
        if cache_path:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump({"head": head, "blocks": blocks, "users": users}, fh)

    print("fetching account data ...")
    accounts = [
        a for a in aave_v3.account_data(rpc, users, chain=chain) if a["debt_usd"] >= min_debt_usd
    ]
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
        calibration.peg_daily_vol = markets.ratio_daily_vol(ratios)
    return calibration


def fetch_depth_quotes(
    rpc: EthRpc,
    chain: ChainConfig,
    reserve: ReserveState,
    dest_symbol: str,
    ladder_usd: tuple[float, ...],
) -> list[tuple[float, float]]:
    """Sell quotes for the reserve asset via whichever aggregator serves the chain."""
    dest = chain.tokens[dest_symbol]
    dest_decimals = aave_v3.token_decimals(rpc, dest)
    sizes_tokens = tuple(usd / reserve.price_usd for usd in ladder_usd)
    if chain.paraswap_network is not None:
        return depth.paraswap_sell_quotes(
            reserve.address,
            dest,
            sizes_tokens,
            src_decimals=reserve.decimals,
            dest_decimals=dest_decimals,
            network=chain.paraswap_network,
        )
    if chain.kyber_slug is not None:
        return depth.kyberswap_sell_quotes(
            chain.kyber_slug,
            reserve.address,
            dest,
            sizes_tokens,
            src_decimals=reserve.decimals,
            dest_decimals=dest_decimals,
        )
    raise RuntimeError(f"no aggregator configured for chain {chain.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chain", default="ethereum", choices=sorted(CHAINS))
    parser.add_argument("--asset", default="wstETH")
    parser.add_argument("--pair-dest", default=None, help="quote destination token symbol")
    parser.add_argument("--blocks", type=int, default=None, help="borrower scan window")
    parser.add_argument("--chunk-blocks", type=int, default=None)
    parser.add_argument("--top", type=int, default=400, help="accounts kept, by debt")
    parser.add_argument("--min-debt", type=float, default=10_000.0)
    parser.add_argument("--ladder-usd", default=None, help="comma-separated quote sizes in USD")
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--kraken-pair", default=None)
    parser.add_argument("--peg-coin", default=None, help="'none' to skip the peg check")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    chain = CHAINS[args.chain]
    if args.asset not in chain.tokens:
        parser.error(f"unknown asset {args.asset!r} on {chain.name}; known: {sorted(chain.tokens)}")
    address = chain.tokens[args.asset]
    rpc = chain.make_rpc()

    blocks = args.blocks or DEFAULT_SCAN_BLOCKS.get(chain.name, 100_000)
    chunk_blocks = args.chunk_blocks or chain.log_chunk_blocks
    dest_symbol = args.pair_dest or ("USDC" if args.asset == "WETH" else "WETH")
    ladder_usd = (
        tuple(float(x) for x in args.ladder_usd.split(","))
        if args.ladder_usd
        else DEFAULT_LADDER_USD
    )
    default_pair, default_peg = ASSET_CALIBRATION.get(args.asset, ("ETHUSD", None))
    kraken_pair = args.kraken_pair or default_pair
    peg_coin = None if args.peg_coin == "none" else (args.peg_coin or default_peg)

    print(f"reading {args.asset} reserve state on {chain.name} ...")
    reserve = build_reserve_state(rpc, chain, args.asset, address)
    print(
        f"  price ${reserve.price_usd:,.2f} | LT {reserve.liquidation_threshold:.2%} "
        f"| bonus {reserve.liquidation_bonus:.2%} | supplied {reserve.total_supplied_tokens:,.0f}"
    )

    cache_path = os.path.join(
        os.path.dirname(__file__), "snapshots", f".borrowers_cache_{chain.name}.json"
    )
    accounts = build_accounts(
        rpc, chain, reserve, blocks, chunk_blocks, args.min_debt, args.top,
        cache_path=cache_path,
    )

    print("calibrating stress from price history ...")
    stress = None
    try:
        stress = build_stress_calibration(kraken_pair, peg_coin, args.lookback_days)
        print(f"  vol {stress.annual_vol:.1%} | t-dof {stress.t_dof} | peg pass {stress.peg_pass}")
    except Exception as exc:  # noqa: BLE001 - degrade to model defaults
        print(f"  WARNING: stress calibration failed ({exc}); snapshot will carry none")

    print("quoting sell ladder for depth calibration ...")
    fitted = None
    try:
        quotes = fetch_depth_quotes(rpc, chain, reserve, dest_symbol, ladder_usd)
        points = depth.quotes_to_slippage_points(quotes, reserve.price_usd)
        source = "paraswap" if chain.paraswap_network is not None else "kyberswap"
        fitted = depth.fit_depth(
            points, reserve.price_usd, source, f"{args.asset}/{dest_symbol}"
        )
        print(f"  ref {fitted.ref_notional_usd / 1e6:,.2f}m @ {fitted.ref_slippage:.3%}")
    except Exception as exc:  # noqa: BLE001 - degrade to model defaults
        print(f"  WARNING: depth calibration failed ({exc}); snapshot will carry none")

    snapshot = MarketSnapshot(
        chain=chain.name,
        block=rpc.block_number(),
        timestamp=int(time.time()),
        reserve=reserve,
        accounts=accounts,
        depth=fitted,
        stress=stress,
        notes=(
            f"Borrowers discovered from Borrow events over the last {blocks:,} blocks; "
            "dormant borrowers outside that window are not sampled."
        ),
        scan_blocks=blocks,
    )
    path = args.out or default_snapshot_path(args.asset, chain.name)
    save_snapshot(snapshot, path)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
