"""Aave V3 Ethereum-mainnet on-chain readers (reserve state, caps, accounts).

Uses raw eth_call with hardcoded 4-byte selectors so the only runtime
dependency is the standard library. Selectors were verified against
4byte.directory and by live probes against mainnet contracts.
"""

from __future__ import annotations

import time

from .rpc import EthRpc

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
    "getUserAccountData(address)": "0xbf92857c",
    "getAssetPrice(address)": "0xb3596f07",
    "balanceOf(address)": "0x70a08231",
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

# getUserAccountData returns HF = 2**256 - 1 for accounts with zero debt.
_NO_DEBT_HF = (1 << 256) - 1


def _addr_arg(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def _words(hex_result: str) -> list[int]:
    raw = hex_result.removeprefix("0x")
    return [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]


def _word_to_address(word: int) -> str:
    return "0x" + f"{word:040x}"


def resolve_contracts(rpc: EthRpc) -> tuple[str, str]:
    """Resolve the current (PoolDataProvider, AaveOracle) from the provider."""
    dp = _word_to_address(_words(rpc.eth_call(ADDRESSES_PROVIDER, SELECTOR["getPoolDataProvider()"]))[0])
    oracle = _word_to_address(_words(rpc.eth_call(ADDRESSES_PROVIDER, SELECTOR["getPriceOracle()"]))[0])
    return dp, oracle


def reserve_configuration(rpc: EthRpc, data_provider: str, asset: str) -> dict:
    w = _words(rpc.eth_call(data_provider, SELECTOR["getReserveConfigurationData(address)"] + _addr_arg(asset)))
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


def reserve_caps(rpc: EthRpc, data_provider: str, asset: str) -> dict:
    w = _words(rpc.eth_call(data_provider, SELECTOR["getReserveCaps(address)"] + _addr_arg(asset)))
    return {"borrow_cap_tokens": float(w[0]), "supply_cap_tokens": float(w[1])}


def reserve_usage(rpc: EthRpc, data_provider: str, asset: str, decimals: int) -> dict:
    w = _words(rpc.eth_call(data_provider, SELECTOR["getReserveData(address)"] + _addr_arg(asset)))
    scale = 10.0**decimals
    return {
        "total_supplied_tokens": w[2] / scale,
        "total_debt_tokens": (w[3] + w[4]) / scale,
    }


def atoken_address(rpc: EthRpc, data_provider: str, asset: str) -> str:
    w = _words(rpc.eth_call(data_provider, SELECTOR["getReserveTokensAddresses(address)"] + _addr_arg(asset)))
    return _word_to_address(w[0])


def variable_debt_token_address(rpc: EthRpc, data_provider: str, asset: str) -> str:
    w = _words(rpc.eth_call(data_provider, SELECTOR["getReserveTokensAddresses(address)"] + _addr_arg(asset)))
    return _word_to_address(w[2])


def asset_price_usd(rpc: EthRpc, oracle: str, asset: str) -> float:
    """Aave oracle price; mainnet base currency is USD with 8 decimals."""
    w = _words(rpc.eth_call(oracle, SELECTOR["getAssetPrice(address)"] + _addr_arg(asset)))
    return w[0] / 1e8


def discover_borrowers(
    rpc: EthRpc,
    from_block: int,
    to_block: int,
    chunk_blocks: int = 5_000,
    pause_s: float = 1.5,
) -> set[str]:
    """Unique onBehalfOf addresses from Borrow events in a block window.

    This sees only recently active borrowers; dormant whales are missed.
    Widen the window (at RPC cost) to reduce that bias.
    """
    users: set[str] = set()
    for start in range(from_block, to_block + 1, chunk_blocks):
        end = min(start + chunk_blocks - 1, to_block)
        logs = rpc.get_logs(POOL, [BORROW_TOPIC0], start, end)
        users.update("0x" + log["topics"][2][26:] for log in logs)
        time.sleep(pause_s)
    return users


def account_data(rpc: EthRpc, users: list[str]) -> list[dict]:
    """Batched Pool.getUserAccountData; base-currency figures in USD."""
    params = [
        [{"to": POOL, "data": SELECTOR["getUserAccountData(address)"] + _addr_arg(u)}, "latest"]
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


def token_balances(rpc: EthRpc, token: str, users: list[str], decimals: int) -> dict[str, float]:
    params = [
        [{"to": token, "data": SELECTOR["balanceOf(address)"] + _addr_arg(u)}, "latest"]
        for u in users
    ]
    scale = 10.0**decimals
    return {u: _words(r)[0] / scale for u, r in zip(users, rpc.batch("eth_call", params))}
