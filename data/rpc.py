"""Minimal stdlib-only JSON-RPC client for public Ethereum endpoints."""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass

# Keyless public endpoints for state reads (eth_call and friends).
DEFAULT_ENDPOINTS: tuple[str, ...] = (
    "https://rpc.mevblocker.io",
    "https://eth.drpc.org",
    "https://ethereum-rpc.publicnode.com",
    "https://1rpc.io/eth",
)

# Endpoints that serve eth_getLogs over wide historical block ranges. The
# second endpoint is a fallback if the first rejects a request.
LOG_ENDPOINTS: tuple[str, ...] = (
    "https://gateway.tenderly.co/public/mainnet",
    "https://rpc.mevblocker.io",
)

_HEADERS = {"Content-Type": "application/json", "User-Agent": "aave-risk-engine/0.1"}


class RpcError(RuntimeError):
    """All endpoints failed or returned a JSON-RPC error."""


@dataclass
class EthRpc:
    """Tiny JSON-RPC client with endpoint failover and batching."""

    endpoints: tuple[str, ...] = DEFAULT_ENDPOINTS
    log_endpoints: tuple[str, ...] = LOG_ENDPOINTS
    timeout: float = 30.0
    retries_per_endpoint: int = 4
    retry_wait_s: float = 1.0
    rate_limit_wait_s: float = 12.0
    batch_size: int = 100

    def _post(self, payload, endpoints: tuple[str, ...] | None = None) -> object:
        body = json.dumps(payload).encode()
        errors: list[str] = []
        for endpoint in endpoints or self.endpoints:
            for attempt in range(self.retries_per_endpoint):
                req = urllib.request.Request(endpoint, data=body, headers=_HEADERS)
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        out = json.loads(resp.read())
                except Exception as exc:  # noqa: BLE001 - network failover
                    errors.append(f"{endpoint}: {exc}")
                    # Rate limiting is transient: wait it out on the same
                    # endpoint rather than failing over to one that may not
                    # support the method at all.
                    rate_limited = "429" in str(exc)
                    wait = self.rate_limit_wait_s if rate_limited else self.retry_wait_s
                    time.sleep(wait * (attempt + 1))
                    continue
                if isinstance(out, dict) and "error" in out:
                    # Method-level errors (rate limits, archive gating) are
                    # endpoint policy, not transient: move to the next endpoint.
                    errors.append(f"{endpoint}: {out['error']}")
                    break
                return out
        raise RpcError("all endpoints failed: " + "; ".join(errors[-4:]))

    def call(self, method: str, params: list) -> object:
        out = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        return out["result"]

    def batch(self, method: str, params_list: list[list]) -> list:
        """Run many calls of one method, preserving order, chunked per request."""
        results: list = []
        for start in range(0, len(params_list), self.batch_size):
            chunk = params_list[start : start + self.batch_size]
            payload = [
                {"jsonrpc": "2.0", "id": i, "method": method, "params": p}
                for i, p in enumerate(chunk)
            ]
            out = self._post(payload)
            if not isinstance(out, list):
                raise RpcError(f"batch response was not a list: {out!r}")
            by_id = {item["id"]: item for item in out}
            for i in range(len(chunk)):
                item = by_id.get(i)
                if item is None or "error" in item:
                    raise RpcError(f"batch item failed: {item!r}")
                results.append(item["result"])
        return results

    @staticmethod
    def block_tag(block: int | str | None = None) -> str:
        if block is None:
            return "latest"
        return hex(block) if isinstance(block, int) else block

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def block_timestamp(self, block: int | str) -> int:
        raw = self.call("eth_getBlockByNumber", [self.block_tag(block), False])
        return int(raw["timestamp"], 16)

    def eth_call(self, to: str, data: str, block: int | str | None = None) -> str:
        return self.call(
            "eth_call", [{"to": to, "data": data}, self.block_tag(block)]
        )

    def try_eth_call(
        self,
        to: str,
        data: str,
        block: int | str | None = None,
        overrides: dict | None = None,
    ) -> tuple[str | None, str]:
        """Call without raising, separating contract answers from outages.

        Returns (result, status) where status is one of:
          "ok"          the call returned data;
          "reverted"    the contract rejected the call, which is evidence
                        that the function is absent or refused it;
          "unavailable" no endpoint gave a definitive answer, which is
                        evidence about the endpoints and nothing else.

        Callers recording contract behaviour must never treat
        "unavailable" as "reverted".

        overrides is an eth_call state-override set. Endpoints that ignore
        it silently return unmodified state, so callers must confirm the
        override took effect on the same endpoint before trusting a result.
        """
        params = [{"to": to, "data": data}, self.block_tag(block)]
        if overrides:
            params.append(overrides)
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": params}
        body = json.dumps(payload).encode()
        for endpoint in self.endpoints:
            req = urllib.request.Request(endpoint, data=body, headers=_HEADERS)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    out = json.loads(resp.read())
            except Exception:  # noqa: BLE001 - try the next endpoint
                time.sleep(self.retry_wait_s)
                continue
            if isinstance(out, dict) and "error" in out:
                message = str(out["error"].get("message", out["error"])).lower()
                if any(
                    marker in message
                    for marker in ("revert", "invalid opcode", "out of gas")
                ):
                    return None, "reverted"
                continue
            result = out.get("result") if isinstance(out, dict) else None
            if result in (None, "0x", ""):
                # A call to a missing function on a contract without a
                # fallback returns empty data rather than reverting.
                return None, "reverted"
            return result, "ok"
        return None, "unavailable"

    def get_logs(self, address: str, topics: list[str], from_block: int, to_block: int) -> list[dict]:
        out = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getLogs",
                "params": [
                    {
                        "address": address,
                        "topics": topics,
                        "fromBlock": hex(from_block),
                        "toBlock": hex(to_block),
                    }
                ],
            },
            endpoints=self.log_endpoints,
        )
        return out["result"]
