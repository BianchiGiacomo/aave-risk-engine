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

# Endpoints that serve eth_getLogs over multi-thousand-block ranges; log
# scans rotate across them to spread rate-limit pressure.
LOG_ENDPOINTS: tuple[str, ...] = (
    "https://eth.drpc.org",
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
    _log_rotation: int = 0

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

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def eth_call(self, to: str, data: str) -> str:
        return self.call("eth_call", [{"to": to, "data": data}, "latest"])

    def get_logs(self, address: str, topics: list[str], from_block: int, to_block: int) -> list[dict]:
        # Rotate the starting endpoint across calls so long scans spread
        # their request rate over every log-capable provider.
        n = len(self.log_endpoints)
        start = self._log_rotation % n
        self._log_rotation += 1
        rotated = self.log_endpoints[start:] + self.log_endpoints[:start]
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
            endpoints=rotated,
        )
        return out["result"]
