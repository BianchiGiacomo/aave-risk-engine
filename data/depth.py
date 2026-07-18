"""Fit the engine's slippage curve to real DEX-aggregator sell quotes.

The engine models fractional slippage as s(q) = q / (L + q) with
q = notional_usd / sqrt(price). Given routed sell quotes at several sizes,
each point implies L_i = q_i * (1 - s_i) / s_i; the calibration takes the
median, which is robust to route discontinuities at particular sizes.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

from .snapshot import DepthCalibration

_HEADERS = {"User-Agent": "aave-risk-engine/0.1"}

DEFAULT_LADDER_TOKENS = (10.0, 50.0, 200.0, 1_000.0, 4_000.0, 12_000.0)


def paraswap_sell_quotes(
    src_token: str,
    dest_token: str,
    sizes_tokens: tuple[float, ...] = DEFAULT_LADDER_TOKENS,
    src_decimals: int = 18,
    dest_decimals: int = 18,
    network: int = 1,
    pause_s: float = 1.0,
    timeout: float = 30.0,
) -> list[tuple[float, float]]:
    """Routed sell quotes: [(size_in_tokens, amount_out_in_dest_tokens)].

    Paraswap rejects quotes above its max-price-impact guard with HTTP 400,
    but the rejection body still carries the best routed ``destAmount``.
    Those should be read as indicative routes beyond the router's normal
    guard -- stress-depth estimates, not guaranteed executable liquidity --
    but they are the only signal for the large-size region the clearance
    test cares about, so they are kept. Sizes with no routed amount at all
    are skipped.
    """
    quotes = []
    for size in sizes_tokens:
        params = urllib.parse.urlencode(
            {
                "srcToken": src_token,
                "destToken": dest_token,
                "amount": str(int(size * 10**src_decimals)),
                "srcDecimals": src_decimals,
                "destDecimals": dest_decimals,
                "side": "SELL",
                "network": network,
            }
        )
        req = urllib.request.Request(f"https://api.paraswap.io/prices?{params}", headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                out = json.loads(exc.read())
            except Exception:  # noqa: BLE001 - non-JSON error body
                out = {}
        dest_raw = out.get("priceRoute", {}).get("destAmount")
        if dest_raw is not None:
            quotes.append((size, float(dest_raw) / 10**dest_decimals))
        time.sleep(pause_s)
    if len(quotes) < 2:
        raise RuntimeError("fewer than two usable quotes; cannot calibrate depth")
    return quotes


def quotes_to_slippage_points(
    quotes: list[tuple[float, float]],
    price_usd: float,
) -> list[list[float]]:
    """Convert (size, out) quotes to [notional_usd, slippage] points.

    The smallest quote defines the mid: aggregator quotes embed the pool
    price, so slippage is measured relative to the best small-size rate
    rather than an external oracle.
    """
    if len(quotes) < 2:
        raise ValueError("need at least two quote sizes")
    quotes = sorted(quotes)
    mid_rate = quotes[0][1] / quotes[0][0]
    points = []
    for size, out in quotes[1:]:
        slip = max(0.0, 1.0 - (out / size) / mid_rate)
        points.append([size * price_usd, slip])
    return points


def fit_depth(
    points: list[list[float]],
    price_usd: float,
    source: str,
    pair: str,
    quoted_at: int | None = None,
    min_slippage: float = 1e-5,
) -> DepthCalibration:
    """Fit L from slippage points and express it as a reference depth point."""
    usable = [(n, s) for n, s in points if s >= min_slippage]
    if not usable:
        raise ValueError("all quoted slippages are below the fit floor")
    implied = [(n / np.sqrt(price_usd)) * (1.0 - s) / s for n, s in usable]
    liquidity = float(np.median(implied))

    ref_notional = max(n for n, _ in usable)
    q_ref = ref_notional / np.sqrt(price_usd)
    ref_slippage = float(q_ref / (liquidity + q_ref))

    return DepthCalibration(
        source=source,
        pair=pair,
        quoted_at=int(quoted_at if quoted_at is not None else time.time()),
        points=[[float(n), float(s)] for n, s in points],
        liquidity=liquidity,
        ref_notional_usd=float(ref_notional),
        ref_slippage=ref_slippage,
    )
