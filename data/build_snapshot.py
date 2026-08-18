"""Build a MarketSnapshot from live public data sources.

Usage:
    python -m aave_risk_engine.data.build_snapshot [--chain ethereum] [--asset wstETH]

Sources (all keyless): public JSON-RPC endpoints for Aave V3 state and
borrower discovery, Paraswap or KyberSwap for routed sell quotes, Kraken
for USD price history, and DefiLlama with a Coingecko fallback for
LST/underlying peg history.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil

from . import aave_v3, depth, markets
from .account_cache import fetch_account_data
from .aave_v3 import CHAINS, ChainConfig
from .borrowers import default_registry_path, update_registry
from .rpc import EthRpc
from .snapshot import (
    AccountRecord,
    BorrowerDiscovery,
    MarketSnapshot,
    ReserveState,
    StressCalibration,
    default_snapshot_path,
    save_snapshot,
)

# Per-asset calibration defaults: (Kraken pair for the USD vol driver,
# market-data asset id for the peg ratio or None when the asset is not
# pegged to a more liquid underlying).
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
MARKET_LADDERS_USD = {
    ("linea", "WETH"): (5e3, 15e3, 41e3, 100e3, 300e3, 1e6),
}


def build_reserve_state(
    rpc: EthRpc, chain: ChainConfig, symbol: str, asset: str, block: int
) -> ReserveState:
    data_provider, oracle = aave_v3.resolve_contracts(rpc, chain, block)
    config = aave_v3.reserve_configuration(rpc, data_provider, asset, block)
    caps = aave_v3.reserve_caps(rpc, data_provider, asset, block)
    usage = aave_v3.reserve_usage(
        rpc, data_provider, asset, config["decimals"], block
    )
    price = aave_v3.asset_price_usd(rpc, oracle, asset, block)
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


def build_accounts(
    rpc: EthRpc,
    chain: ChainConfig,
    reserve: ReserveState,
    users: list[str],
    block: int,
    min_debt_usd: float,
    top_n: int | None,
    account_cache_path: str | None = None,
    account_request_batch_size: int = 200,
) -> tuple[list[AccountRecord], int, int]:
    data_provider, oracle = aave_v3.resolve_contracts(rpc, chain, block)
    weth = chain.tokens["WETH"]
    weth_price = aave_v3.asset_price_usd(rpc, oracle, weth, block)

    print(f"fetching account data for {len(users):,} historical borrowers ...")
    if account_cache_path:
        raw_accounts, users = fetch_account_data(
            rpc,
            users,
            chain,
            block,
            account_cache_path,
            request_batch_size=account_request_batch_size,
        )
    else:
        raw_accounts = aave_v3.account_data(rpc, users, chain=chain, block=block)
    accounts = [
        a
        for a in raw_accounts
        if a["debt_usd"] >= min_debt_usd
    ]
    accounts.sort(key=lambda a: a["debt_usd"], reverse=True)
    active_count = len(accounts)
    print(f"  {active_count:,} accounts with debt >= ${min_debt_usd:,.0f}")

    print(f"fetching {reserve.symbol} collateral and WETH debt balances ...")
    addresses = [a["address"] for a in accounts]
    target_data = aave_v3.user_reserve_data(
        rpc, data_provider, reserve.address, addresses, reserve.decimals, block=block
    )
    weth_data = aave_v3.user_reserve_data(
        rpc, data_provider, weth, addresses, 18, block=block
    )
    records = [
        AccountRecord(
            address=a["address"],
            collateral_usd=a["collateral_usd"],
            debt_usd=a["debt_usd"],
            avg_liquidation_threshold=a["avg_liquidation_threshold"],
            health_factor=a["health_factor"],
            target_collateral_usd=(
                target_data[a["address"]]["atoken_balance"] * reserve.price_usd
                if target_data[a["address"]]["collateral_enabled"]
                else 0.0
            ),
            eth_debt_usd=(
                weth_data[a["address"]]["stable_debt"]
                + weth_data[a["address"]]["variable_debt"]
            )
            * weth_price,
        )
        for a in accounts
    ]
    if top_n is not None:
        records = records[:top_n]
    return records, active_count, len(users)


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
        try:
            ratios = markets.defillama_ratio_history(peg_coin, "ethereum", lookback_days)
            calibration.source = calibration.source.replace(
                f"coingecko:{peg_coin}", f"defillama:{peg_coin}"
            )
        except Exception:  # noqa: BLE001 - fall back to the secondary source
            ratios = markets.coingecko_ratio_history(peg_coin, "eth", lookback_days)
        ratios = markets.clean_lst_ratio(ratios)
        peg = markets.arfc_peg_check(ratios)
        calibration.peg_pass = peg["peg_pass"]
        calibration.peg_worst_deviation = peg["peg_worst_deviation"]
        calibration.peg_max_run_days = peg["peg_max_run_days"]
        calibration.peg_daily_vol = markets.ratio_daily_vol(ratios)
        calibration.peg_mean_reversion_speed = markets.peg_mean_reversion_speed(ratios)
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


def build_market_snapshot(
    chain_name: str = "ethereum",
    asset: str = "wstETH",
    pair_dest: str | None = None,
    chunk_blocks: int | None = None,
    top: int | None = None,
    min_debt_usd: float = 10_000.0,
    ladder_usd: tuple[float, ...] | None = None,
    lookback_days: int = 365,
    kraken_pair: str | None = None,
    peg_coin: str | None = None,
    calibrate_peg: bool = True,
    borrower_registry_path: str | None = None,
    rebuild_borrower_registry: bool = False,
    log_pause_s: float = 0.0,
    registry_checkpoint_chunks: int = 10,
    rpc_batch_size: int = 1_000,
    account_cache_dir: str | None = None,
    account_request_batch_size: int = 200,
    block: int | None = None,
    rpc_timeout: float = 30.0,
    rpc_retries: int = 4,
) -> MarketSnapshot:
    """Build a live snapshot without persisting it.

    The CLI saves the returned object. Interactive callers can instead keep
    it in memory and offer the serialized JSON as a download.
    """
    if chain_name not in CHAINS:
        raise ValueError(f"unknown chain {chain_name!r}; known: {sorted(CHAINS)}")
    chain = CHAINS[chain_name]
    if asset not in chain.tokens:
        raise ValueError(f"unknown asset {asset!r} on {chain.name}; known: {sorted(chain.tokens)}")

    address = chain.tokens[asset]
    rpc = chain.make_rpc()
    rpc.timeout = rpc_timeout
    rpc.retries_per_endpoint = rpc_retries
    rpc.batch_size = rpc_batch_size
    log_chunk = chunk_blocks or chain.log_chunk_blocks
    dest_symbol = pair_dest or ("USDC" if asset == "WETH" else "WETH")
    quote_ladder = ladder_usd or MARKET_LADDERS_USD.get(
        (chain.name, asset), DEFAULT_LADDER_USD
    )
    default_pair, default_peg = ASSET_CALIBRATION.get(asset, ("ETHUSD", None))
    resolved_pair = kraken_pair or default_pair
    resolved_peg = (peg_coin or default_peg) if calibrate_peg else None
    head = rpc.block_number()
    block = head if block is None else block
    if block > head:
        raise ValueError(f"requested block {block:,} is above chain head {head:,}")
    timestamp = rpc.block_timestamp(block)

    registry_path = borrower_registry_path or default_registry_path(chain.name)
    print(
        f"updating complete borrower registry from block "
        f"{chain.borrower_registry_start_block:,} through {block:,} ..."
    )
    registry = update_registry(
        rpc,
        chain,
        block,
        registry_path,
        chunk_blocks=log_chunk,
        pause_s=log_pause_s,
        rebuild=rebuild_borrower_registry,
        checkpoint_chunks=registry_checkpoint_chunks,
    )
    print(f"  {len(registry.users):,} unique historical borrowers")

    print(f"reading {asset} reserve state on {chain.name} ...")
    reserve = build_reserve_state(rpc, chain, asset, address, block)
    print(
        f"  price ${reserve.price_usd:,.2f} | LT {reserve.liquidation_threshold:.2%} "
        f"| bonus {reserve.liquidation_bonus:.2%} | supplied {reserve.total_supplied_tokens:,.0f}"
    )
    registry_fingerprint = hashlib.sha256(
        "\n".join(registry.users).encode("ascii")
    ).hexdigest()[:12]
    account_cache_path = (
        os.path.join(
            account_cache_dir,
            f"accounts_{chain.name}_{block}_{registry_fingerprint}.sqlite3",
        )
        if account_cache_dir
        else None
    )
    accounts, active_count, candidate_count = build_accounts(
        rpc,
        chain,
        reserve,
        registry.users,
        block,
        min_debt_usd,
        top,
        account_cache_path=account_cache_path,
        account_request_batch_size=account_request_batch_size,
    )

    print("calibrating stress from price history ...")
    stress = None
    try:
        stress = build_stress_calibration(resolved_pair, resolved_peg, lookback_days)
        print(f"  vol {stress.annual_vol:.1%} | t-dof {stress.t_dof} | peg pass {stress.peg_pass}")
    except Exception as exc:  # noqa: BLE001 - preserve partial live snapshots
        print(f"  WARNING: stress calibration failed ({exc}); snapshot will carry none")

    print("quoting sell ladder for depth calibration ...")
    fitted = None
    try:
        quotes = fetch_depth_quotes(rpc, chain, reserve, dest_symbol, quote_ladder)
        points = depth.quotes_to_slippage_points(quotes, reserve.price_usd)
        source = "paraswap" if chain.paraswap_network is not None else "kyberswap"
        fitted = depth.fit_depth(points, reserve.price_usd, source, f"{asset}/{dest_symbol}")
        largest_quote = max(fitted.points, key=lambda point: point[0])
        print(
            f"  empirical max {largest_quote[0] / 1e6:,.2f}m "
            f"@ {largest_quote[1]:.3%}"
        )
    except Exception as exc:  # noqa: BLE001 - preserve partial live snapshots
        print(f"  WARNING: depth calibration failed ({exc}); snapshot will carry none")

    return MarketSnapshot(
        chain=chain.name,
        block=block,
        timestamp=timestamp,
        reserve=reserve,
        accounts=accounts,
        depth=fitted,
        stress=stress,
        borrower_discovery=BorrowerDiscovery(
            source="complete Borrow-event registry",
            from_block=registry.from_block,
            to_block=block,
            candidate_count=candidate_count,
            active_count=active_count,
            min_debt_usd=min_debt_usd,
            account_limit=top,
        ),
        notes=(
            f"Complete Borrow-event registry from Pool deployment block "
            f"{registry.from_block:,} through snapshot block {block:,}; "
            f"{candidate_count:,} historical candidates queried at the pinned block."
        ),
        scan_blocks=None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chain", default="ethereum", choices=sorted(CHAINS))
    parser.add_argument("--asset", default="wstETH")
    parser.add_argument("--pair-dest", default=None, help="quote destination token symbol")
    parser.add_argument("--chunk-blocks", type=int, default=None)
    parser.add_argument(
        "--top", type=int, default=None, help="optional output cap, ranked by total debt"
    )
    parser.add_argument("--min-debt", type=float, default=10_000.0)
    parser.add_argument("--ladder-usd", default=None, help="comma-separated quote sizes in USD")
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--kraken-pair", default=None)
    parser.add_argument("--peg-coin", default=None, help="'none' to skip the peg check")
    parser.add_argument("--rpc-timeout", type=float, default=30.0)
    parser.add_argument("--rpc-retries", type=int, default=4)
    parser.add_argument("--rpc-batch-size", type=int, default=1_000)
    parser.add_argument(
        "--account-cache-dir",
        default=None,
        help="SQLite checkpoint directory for resumable block-pinned account reads",
    )
    parser.add_argument("--account-request-batch-size", type=int, default=200)
    parser.add_argument("--block", type=int, default=None, help="pin all on-chain reads")
    parser.add_argument(
        "--borrower-registry",
        default=None,
        help="complete registry JSON; defaults to data/borrowers/aave_v3_<chain>.json",
    )
    parser.add_argument(
        "--registry-out",
        default=None,
        help="update a copy of --borrower-registry instead of modifying the source",
    )
    parser.add_argument("--rebuild-borrower-registry", action="store_true")
    parser.add_argument("--registry-checkpoint-chunks", type=int, default=10)
    parser.add_argument("--log-pause", type=float, default=0.0)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    ladder_usd = (
        tuple(float(x) for x in args.ladder_usd.split(","))
        if args.ladder_usd
        else None
    )
    registry_source = args.borrower_registry or default_registry_path(args.chain)
    registry_path = args.registry_out or registry_source
    if args.registry_out and not os.path.exists(args.registry_out):
        if not os.path.exists(registry_source):
            parser.error("--registry-out requires an existing source registry")
        directory = os.path.dirname(args.registry_out)
        if directory:
            os.makedirs(directory, exist_ok=True)
        shutil.copyfile(registry_source, args.registry_out)
    snapshot = build_market_snapshot(
        chain_name=args.chain,
        asset=args.asset,
        pair_dest=args.pair_dest,
        chunk_blocks=args.chunk_blocks,
        top=args.top,
        min_debt_usd=args.min_debt,
        ladder_usd=ladder_usd,
        lookback_days=args.lookback_days,
        kraken_pair=args.kraken_pair,
        peg_coin=None if args.peg_coin == "none" else args.peg_coin,
        calibrate_peg=args.peg_coin != "none",
        borrower_registry_path=registry_path,
        rebuild_borrower_registry=args.rebuild_borrower_registry,
        log_pause_s=args.log_pause,
        registry_checkpoint_chunks=args.registry_checkpoint_chunks,
        rpc_batch_size=args.rpc_batch_size,
        account_cache_dir=args.account_cache_dir,
        account_request_batch_size=args.account_request_batch_size,
        block=args.block,
        rpc_timeout=args.rpc_timeout,
        rpc_retries=args.rpc_retries,
    )
    path = args.out or default_snapshot_path(args.asset, args.chain)
    save_snapshot(snapshot, path)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
