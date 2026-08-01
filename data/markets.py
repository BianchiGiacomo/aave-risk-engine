"""Price history, realized-volatility calibration, and the ARFC peg rule.

Sources are keyless public APIs: Kraken for USD price history and
DefiLlama with a Coingecko fallback for LST/underlying ratio history.
"""

from __future__ import annotations

import json
import urllib.request

import numpy as np

_HEADERS = {"User-Agent": "aave-risk-engine/0.1"}

# The ARFC Aave Risk Framework E-Mode precondition for pegged assets:
# no sustained deviation from collateral value of >= 1% over >= 2 days.
ARFC_PEG_THRESHOLD = 0.01
ARFC_PEG_WINDOW_DAYS = 2.0


def _get_json(url: str, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def kraken_daily_closes(pair: str = "ETHUSD", timeout: float = 30.0) -> np.ndarray:
    """Daily close history (up to ~720 days) from Kraken's public OHLC API."""
    out = _get_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440", timeout)
    if out.get("error"):
        raise RuntimeError(f"kraken error: {out['error']}")
    key = next(k for k in out["result"] if k != "last")
    rows = out["result"][key]
    return np.array([float(r[4]) for r in rows])


def coingecko_ratio_history(
    coin_id: str = "staked-ether",
    vs: str = "eth",
    days: int = 365,
    timeout: float = 30.0,
) -> np.ndarray:
    """Daily price of an LST in units of its underlying (e.g. stETH/ETH)."""
    url = (
        f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
        f"?vs_currency={vs}&days={days}&interval=daily"
    )
    out = _get_json(url, timeout)
    return np.array([p[1] for p in out["prices"]])


def _defillama_daily(key: str, days: int, timeout: float = 30.0) -> dict:
    import datetime as dt
    import time as time_mod

    start = int(time_mod.time()) - days * 86_400
    url = f"https://coins.llama.fi/chart/{key}?start={start}&span={days}&period=1d"
    out = _get_json(url, timeout)
    series = out.get("coins", {}).get(key, {}).get("prices", [])
    return {
        dt.datetime.fromtimestamp(p["timestamp"], dt.timezone.utc).date(): float(p["price"])
        for p in series
    }


def defillama_ratio_history(
    coin_id: str = "staked-ether",
    vs_id: str = "ethereum",
    days: int = 365,
    timeout: float = 30.0,
) -> np.ndarray:
    """LST/underlying daily ratio from DefiLlama, day-aligned.

    Primary peg source: Coingecko's historical LST/underlying series was
    observed returning corrupted segments (ratios far above par, phantom
    multi-day depegs contradicted by an earlier fetch of the same dates),
    while the DefiLlama marks stayed consistent.
    """
    lst = _defillama_daily(f"coingecko:{coin_id}", days, timeout)
    drv = _defillama_daily(f"coingecko:{vs_id}", days, timeout)
    shared = sorted(set(lst) & set(drv))
    if len(shared) < 30:
        raise RuntimeError("too few aligned days for the ratio history")
    return np.array([lst[d] / drv[d] for d in shared])


def rolling_median3(values: np.ndarray) -> np.ndarray:
    """Three-point rolling median, endpoints kept as-is.

    Daily marks for two coins are not sampled at the same instant, so
    their ratio carries single-day misalignment noise; a short median
    removes single-mark spikes in both directions without reshaping
    sustained moves.
    """
    values = np.asarray(values, dtype=float)
    if values.size < 3:
        return values.copy()
    stacked = np.stack([values[:-2], values[1:-1], values[2:]])
    out = values.copy()
    out[1:-1] = np.median(stacked, axis=0)
    return out


def clean_lst_ratio(ratios: np.ndarray) -> np.ndarray:
    """Hygiene for LST/underlying ratio series before calibration.

    Caps at par (minting enforces a hard ceiling at 1.0, so prints above
    it are data artifacts) and applies a three-point rolling median
    against single-mark spikes.
    """
    return rolling_median3(np.minimum(np.asarray(ratios, dtype=float), 1.0))


def realized_annual_vol(closes: np.ndarray, lookback_days: int | None = None) -> float:
    closes = np.asarray(closes, dtype=float)
    if lookback_days is not None:
        closes = closes[-(lookback_days + 1) :]
    rets = np.diff(np.log(closes))
    if rets.size < 30:
        raise ValueError("need at least 30 daily returns")
    return float(rets.std(ddof=1) * np.sqrt(365.0))


def fit_student_t_dof(
    closes: np.ndarray,
    lookback_days: int | None = None,
    lo: float = 2.6,
    hi: float = 12.0,
) -> float | None:
    """Match Student-t degrees of freedom to sample excess kurtosis.

    For a t distribution with dof > 4, excess kurtosis is 6 / (dof - 4).
    Returns None when tails look Gaussian or thinner (no t needed).
    """
    closes = np.asarray(closes, dtype=float)
    if lookback_days is not None:
        closes = closes[-(lookback_days + 1) :]
    rets = np.diff(np.log(closes))
    if rets.size < 60:
        raise ValueError("need at least 60 daily returns")
    z = (rets - rets.mean()) / rets.std(ddof=1)
    excess = float((z**4).mean() - 3.0)
    if excess <= 0.1:
        return None
    return float(np.clip(4.0 + 6.0 / excess, lo, hi))


def ratio_daily_vol(ratios: np.ndarray) -> float:
    """Daily volatility of an LST/underlying ratio series (log changes)."""
    ratios = np.asarray(ratios, dtype=float)
    rets = np.diff(np.log(ratios))
    if rets.size < 30:
        raise ValueError("need at least 30 daily ratio changes")
    return float(rets.std(ddof=1))


def arfc_peg_check(
    ratios: np.ndarray,
    threshold: float = ARFC_PEG_THRESHOLD,
    window_days: float = ARFC_PEG_WINDOW_DAYS,
    samples_per_day: float = 1.0,
) -> dict:
    """Apply the ARFC pegged-asset rule to a ratio series (1.0 = perfect peg).

    Fails if any deviation of at least `threshold` below peg is sustained for
    `window_days` or longer.
    """
    ratios = np.asarray(ratios, dtype=float)
    deviation = np.maximum(0.0, 1.0 - ratios)
    breached = deviation >= threshold

    longest_run = 0
    run = 0
    for flag in breached:
        run = run + 1 if flag else 0
        longest_run = max(longest_run, run)
    longest_days = longest_run / samples_per_day

    return {
        "peg_pass": bool(longest_days < window_days),
        "peg_worst_deviation": float(deviation.max()) if deviation.size else 0.0,
        "peg_max_run_days": float(longest_days),
    }
