"""Aave V3 on-chain readers (reserve state, caps, accounts) per chain.

Uses raw eth_call with hardcoded 4-byte selectors so the only runtime
dependency is the standard library. Selectors were verified against
4byte.directory and by live probes; per-chain contract addresses come from
the BGD aave-address-book and were verified by live probes as well.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .rpc import DEFAULT_ENDPOINTS, LOG_ENDPOINTS, EthRpc, RpcError

POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
ADDRESSES_PROVIDER = "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e"

# keccak("Borrow(address,address,address,uint256,uint8,uint256,uint16)")
BORROW_TOPIC0 = "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0"

SELECTOR = {
    "getPoolDataProvider()": "0xe860accb",
    "getPriceOracle()": "0xfca513a8",
    "getReserveConfigurationData(address)": "0x3e150141",
    "getReserveCaps(address)": "0x46fbe558",
    "getReserveData(address)": "0x35ea6a75",
    "getReserveTokensAddresses(address)": "0xd2493b6c",
    "getUserReserveData(address,address)": "0x28dd2d01",
    "getUserAccountData(address)": "0xbf92857c",
    "getAssetPrice(address)": "0xb3596f07",
    "balanceOf(address)": "0x70a08231",
    "decimals()": "0x313ce567",
}

# Well-known mainnet token addresses (checksummed).
TOKENS = {
    "wstETH": "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0",
    "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "DAI": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
}


@dataclass(frozen=True)
class ChainConfig:
    """Everything chain-specific: contracts, RPC endpoints, aggregators."""

    name: str
    addresses_provider: str
    pool: str
    rpc_endpoints: tuple[str, ...]
    log_endpoints: tuple[str, ...]
    borrower_registry_start_block: int
    tokens: dict[str, str] = field(default_factory=dict)
    log_chunk_blocks: int = 5_000
    block_time_s: float = 12.0
    # Aggregator support for depth quotes; None means unsupported.
    paraswap_network: int | None = None
    kyber_slug: str | None = None

    def make_rpc(self) -> EthRpc:
        return EthRpc(endpoints=self.rpc_endpoints, log_endpoints=self.log_endpoints)


CHAINS: dict[str, ChainConfig] = {
    "ethereum": ChainConfig(
        name="ethereum",
        addresses_provider=ADDRESSES_PROVIDER,
        pool=POOL,
        rpc_endpoints=DEFAULT_ENDPOINTS,
        log_endpoints=LOG_ENDPOINTS,
        borrower_registry_start_block=16_291_127,
        tokens=TOKENS,
        log_chunk_blocks=100_000,
        paraswap_network=1,
        kyber_slug="ethereum",
    ),
    # Addresses from bgd-labs/aave-address-book AaveV3Linea.sol.
    "linea": ChainConfig(
        name="linea",
        addresses_provider="0x89502c3731F69DDC95B65753708A07F8Cd0373F4",
        pool="0xc47b8C00b0f69a36fa203Ffeac0334874574a8Ac",
        rpc_endpoints=(
            "https://rpc.linea.build",
            "https://linea-rpc.publicnode.com",
            "https://linea.drpc.org",
            "https://1rpc.io/linea",
        ),
        # Tenderly serves wide archive ranges; the official public RPC is the
        # 10k-block fallback used by the adaptive splitter.
        log_endpoints=(
            "https://gateway.tenderly.co/public/linea",
            "https://rpc.linea.build",
        ),
        borrower_registry_start_block=12_430_836,
        tokens={
            "WETH": "0xe5D7C2a44FfDDf6b295A15c148167daaAf5Cf34f",
            "WBTC": "0x3aAB2285ddcDdaD8edf438C1bAB47e1a9D05a9b4",
            "USDC": "0x176211869cA2b568f2A7D4EE941E073a821EE1ff",
            "USDT": "0xA219439258ca9da29E9Cc4cE5596924745e12B93",
            "wstETH": "0xB5beDd42000b71FddE22D3eE8a79Bd49A568fC8F",
            "weETH": "0x1Bf74C010E6320bab11e2e5A532b5AC15e0b8aA6",
        },
        log_chunk_blocks=1_000_000,
        block_time_s=2.0,
        paraswap_network=None,
        kyber_slug="linea",
    ),
}

# getUserAccountData returns HF = 2**256 - 1 for accounts with zero debt.
_NO_DEBT_HF = (1 << 256) - 1


def _addr_arg(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def _words(hex_result: str) -> list[int]:
    raw = hex_result.removeprefix("0x")
    return [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]


def _word_to_address(word: int) -> str:
    return "0x" + f"{word:040x}"


def resolve_contracts(
    rpc: EthRpc,
    chain: ChainConfig | None = None,
    block: int | str | None = None,
) -> tuple[str, str]:
    """Resolve the (PoolDataProvider, AaveOracle) at one block."""
    provider = (chain or CHAINS["ethereum"]).addresses_provider
    dp = _word_to_address(
        _words(rpc.eth_call(provider, SELECTOR["getPoolDataProvider()"], block))[0]
    )
    oracle = _word_to_address(
        _words(rpc.eth_call(provider, SELECTOR["getPriceOracle()"], block))[0]
    )
    return dp, oracle


def reserve_configuration(
    rpc: EthRpc, data_provider: str, asset: str, block: int | str | None = None
) -> dict:
    w = _words(
        rpc.eth_call(
            data_provider,
            SELECTOR["getReserveConfigurationData(address)"] + _addr_arg(asset),
            block,
        )
    )
    return {
        "decimals": w[0],
        "ltv": w[1] / 1e4,
        "liquidation_threshold": w[2] / 1e4,
        # On-chain bonus is stored as a multiplier in bps, e.g. 10600 -> 6%.
        "liquidation_bonus": w[3] / 1e4 - 1.0,
        "reserve_factor": w[4] / 1e4,
        "usage_as_collateral": bool(w[5]),
        "borrowing_enabled": bool(w[6]),
        "is_active": bool(w[8]),
        "is_frozen": bool(w[9]),
    }


def reserve_caps(
    rpc: EthRpc, data_provider: str, asset: str, block: int | str | None = None
) -> dict:
    w = _words(
        rpc.eth_call(
            data_provider, SELECTOR["getReserveCaps(address)"] + _addr_arg(asset), block
        )
    )
    return {"borrow_cap_tokens": float(w[0]), "supply_cap_tokens": float(w[1])}


def reserve_usage(
    rpc: EthRpc,
    data_provider: str,
    asset: str,
    decimals: int,
    block: int | str | None = None,
) -> dict:
    w = _words(
        rpc.eth_call(
            data_provider, SELECTOR["getReserveData(address)"] + _addr_arg(asset), block
        )
    )
    scale = 10.0**decimals
    return {
        "total_supplied_tokens": w[2] / scale,
        "total_debt_tokens": (w[3] + w[4]) / scale,
    }


def atoken_address(
    rpc: EthRpc, data_provider: str, asset: str, block: int | str | None = None
) -> str:
    w = _words(
        rpc.eth_call(
            data_provider,
            SELECTOR["getReserveTokensAddresses(address)"] + _addr_arg(asset),
            block,
        )
    )
    return _word_to_address(w[0])


def variable_debt_token_address(
    rpc: EthRpc, data_provider: str, asset: str, block: int | str | None = None
) -> str:
    w = _words(
        rpc.eth_call(
            data_provider,
            SELECTOR["getReserveTokensAddresses(address)"] + _addr_arg(asset),
            block,
        )
    )
    return _word_to_address(w[2])


def token_decimals(
    rpc: EthRpc, token: str, block: int | str | None = None
) -> int:
    return _words(rpc.eth_call(token, SELECTOR["decimals()"], block))[0]


def asset_price_usd(
    rpc: EthRpc, oracle: str, asset: str, block: int | str | None = None
) -> float:
    """Aave oracle price; mainnet base currency is USD with 8 decimals."""
    w = _words(
        rpc.eth_call(
            oracle, SELECTOR["getAssetPrice(address)"] + _addr_arg(asset), block
        )
    )
    return w[0] / 1e8


def discover_borrowers(
    rpc: EthRpc,
    from_block: int,
    to_block: int,
    chunk_blocks: int = 5_000,
    pause_s: float = 0.0,
    chain: ChainConfig | None = None,
) -> set[str]:
    """Unique onBehalfOf addresses from Borrow events in an inclusive range."""
    pool = (chain or CHAINS["ethereum"]).pool
    users: set[str] = set()
    for start in range(from_block, to_block + 1, chunk_blocks):
        end = min(start + chunk_blocks - 1, to_block)
        logs = _borrow_logs(rpc, pool, start, end)
        users.update("0x" + log["topics"][2][26:] for log in logs)
        if pause_s > 0.0:
            time.sleep(pause_s)
    return users


def _borrow_logs(
    rpc: EthRpc, pool: str, from_block: int, to_block: int
) -> list[dict]:
    """Read logs, splitting ranges when a public provider rejects the span."""
    try:
        return rpc.get_logs(pool, [BORROW_TOPIC0], from_block, to_block)
    except RpcError as exc:
        message = str(exc).lower()
        range_limited = "range" in message or "limited to" in message
        if from_block >= to_block or not range_limited:
            raise
        midpoint = (from_block + to_block) // 2
        return _borrow_logs(rpc, pool, from_block, midpoint) + _borrow_logs(
            rpc, pool, midpoint + 1, to_block
        )


def account_data(
    rpc: EthRpc,
    users: list[str],
    chain: ChainConfig | None = None,
    block: int | str | None = None,
) -> list[dict]:
    """Batched Pool.getUserAccountData; base-currency figures in USD."""
    pool = (chain or CHAINS["ethereum"]).pool
    params = [
        [
            {"to": pool, "data": SELECTOR["getUserAccountData(address)"] + _addr_arg(u)},
            rpc.block_tag(block),
        ]
        for u in users
    ]
    out = []
    for user, res in zip(users, rpc.batch("eth_call", params)):
        w = _words(res)
        out.append(
            {
                "address": user,
                "collateral_usd": w[0] / 1e8,
                "debt_usd": w[1] / 1e8,
                "avg_liquidation_threshold": w[3] / 1e4,
                "health_factor": float("inf") if w[5] == _NO_DEBT_HF else w[5] / 1e18,
            }
        )
    return out


def token_balances(
    rpc: EthRpc,
    token: str,
    users: list[str],
    decimals: int,
    block: int | str | None = None,
) -> dict[str, float]:
    params = [
        [
            {"to": token, "data": SELECTOR["balanceOf(address)"] + _addr_arg(u)},
            rpc.block_tag(block),
        ]
        for u in users
    ]
    scale = 10.0**decimals
    return {u: _words(r)[0] / scale for u, r in zip(users, rpc.batch("eth_call", params))}


def user_reserve_data(
    rpc: EthRpc,
    data_provider: str,
    asset: str,
    users: list[str],
    decimals: int,
    block: int | str | None = None,
) -> dict[str, dict]:
    """Per-user reserve balances and collateral-use flag at one block."""
    params = [
        [
            {
                "to": data_provider,
                "data": (
                    SELECTOR["getUserReserveData(address,address)"]
                    + _addr_arg(asset)
                    + _addr_arg(user)
                ),
            },
            rpc.block_tag(block),
        ]
        for user in users
    ]
    scale = 10.0**decimals
    output = {}
    for user, raw in zip(users, rpc.batch("eth_call", params)):
        words = _words(raw)
        output[user] = {
            "atoken_balance": words[0] / scale,
            "stable_debt": words[1] / scale,
            "variable_debt": words[2] / scale,
            "collateral_enabled": bool(words[8]),
        }
    return output
