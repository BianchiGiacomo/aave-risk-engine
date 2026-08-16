"""Market report: real Aave V3 state vs model-recommended exposure.

Usage:
    python -m aave_risk_engine.run_market_report [--snapshot path] [--budget 5e6]
        [--figure [path]]

Loads a committed MarketSnapshot (build a fresh one with
``python -m aave_risk_engine.data.build_snapshot``), runs the risk engine on
the real borrower book with calibrated parameters, and prints the decision
view: current caps and usage, model-safe exposure, and the ARFC
largest-borrower clearance test.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import sys

import matplotlib.pyplot as plt

from . import plotting
from .config import SimConfig
from .data import (
    arfc_clearance_test,
    build_real_book,
    default_snapshot_path,
    load_snapshot,
)
from .data.aave_v3 import CHAINS
from .data.book import scenario_config_from_snapshot
from .engine import RiskEngine


def _fmt(x: float) -> str:
    for unit, div in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(x) >= div:
            return f"${x / div:,.2f}{unit}"
    return f"${x:,.2f}" if 0 < abs(x) < 1 else f"${x:,.0f}"


def _print_tail_diagnostics(result) -> None:
    probability_low, probability_high = result.prob_bad_debt_interval()
    conditional = result.conditional_mean_bad_debt
    conditional_text = _fmt(conditional) if conditional is not None else "n/a"
    print(
        f"  positive draws: {result.positive_loss_count:,} / "
        f"{result.bad_debt.size:,}"
    )
    print(
        f"  P(bad debt)  : {result.prob_bad_debt:.3%} "
        f"(95% Wilson CI {probability_low:.3%} to {probability_high:.3%})"
    )
    print(f"  expected loss: {_fmt(result.mean)}")
    print(f"  loss severity: {conditional_text} conditional on positive loss")
    print(f"  VaR99        : {_fmt(result.var)}")
    print(
        f"  CVaR99       : {_fmt(result.cvar)} "
        f"(worst {result.cvar_tail_count:,} draws)"
    )
    if result.positive_loss_count < 30:
        print(
            "  WARNING: fewer than 30 positive-loss draws; CVaR and conditional "
            "severity are low-sample estimates"
        )


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_safe(value):
    if dataclasses.is_dataclass(value):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_manifest(
    path: str,
    snapshot_path: str,
    snapshot,
    config,
    args,
    book,
    base,
    combined_book,
    combined,
    share_sensitivity: list[dict],
    recommendation: dict,
    clearance,
) -> str:
    project_dir = os.path.dirname(__file__)
    sweep = recommendation["sweep"]
    books = {
        "usd_debt": {
            "accounts": int(book.debt_usd.size),
            "debt_usd": book.total_debt,
            "metrics": base.diagnostics(),
        }
    }
    if combined is not None and combined_book is not None:
        books["combined"] = {
            "accounts": int(combined_book.debt_usd.size),
            "debt_usd": combined_book.total_debt,
            "eth_debt_usd": float(combined_book.eth_debt_usd.sum()),
            "metrics": combined.diagnostics(),
        }
    parameters = dataclasses.asdict(config)
    parameters.pop("positions", None)
    manifest = {
        "schema_version": 1,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "command": [
            "python",
            "-m",
            "aave_risk_engine.run_market_report",
            *sys.argv[1:],
        ],
        "snapshot": {
            "file": os.path.relpath(snapshot_path, project_dir).replace("\\", "/"),
            "sha256": _sha256(snapshot_path),
            "chain": snapshot.chain,
            "block": snapshot.block,
            "timestamp": snapshot.timestamp,
            "asset": snapshot.reserve.symbol,
        },
        "run": {
            "n_scenarios": args.n_scenarios,
            "seed": args.seed,
            "cvar_level": config.sim.cvar_level,
            "budget_usd": args.budget,
            "min_target_share": args.min_target_share,
            "queue": "ordered" if args.ordered else "aggregate",
            "estimator": "plain_monte_carlo",
        },
        "parameters": parameters,
        "books": books,
        "target_share_sensitivity": share_sensitivity,
        "cap_recommendation": {
            "budget_usd": recommendation["budget"],
            "recommended_debt_exposure_usd": recommendation["recommended_cap"],
            "sweep": {
                "debt_exposure_usd": sweep["caps"],
                "expected_bad_debt_usd": sweep["mean"],
                "cvar_usd": sweep["cvar"],
                "var_usd": sweep["var"],
                "prob_bad_debt": sweep["prob"],
            },
        },
        "clearance": clearance,
    }
    absolute_path = os.path.abspath(path)
    os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
    with open(absolute_path, "w", encoding="utf-8") as fh:
        json.dump(_json_safe(manifest), fh, indent=2, allow_nan=False)
        fh.write("\n")
    return absolute_path


def _write_depth_figure(snapshot, requested_path: str, clearance) -> str:
    depth = snapshot.depth
    if depth is None:
        raise ValueError("snapshot has no depth calibration")

    path = requested_path or os.path.join(
        os.path.dirname(__file__),
        "docs",
        "assets",
        f"{snapshot.chain}_{snapshot.reserve.symbol.lower()}_depth.png",
    )
    labels = {"kyberswap": "KyberSwap", "paraswap": "ParaSwap"}
    source = labels.get(depth.source.lower(), depth.source)
    is_linea_weth = (
        snapshot.chain.lower() == "linea"
        and snapshot.reserve.symbol.upper() == "WETH"
    )
    marker_notional = 41_000.0 if is_linea_weth else None
    marker_label = "LlamaRisk reference ($41k)" if is_linea_weth else None
    additional_markers = (
        [
            (
                clearance.largest_borrower_usd,
                f"largest borrower sale ({_fmt(clearance.largest_borrower_usd)})",
            )
        ]
        if is_linea_weth
        else None
    )
    break_even = snapshot.reserve.liquidation_bonus / (
        1.0 + snapshot.reserve.liquidation_bonus
    )
    fig = plotting.plot_empirical_depth_curve(
        depth.points,
        break_even,
        marker_notional_usd=marker_notional,
        marker_label=marker_label,
        additional_markers=additional_markers,
        quote_label=f"{source} quotes",
        title=f"{snapshot.chain.title()} {snapshot.reserve.symbol} empirical depth",
    )
    absolute_path = os.path.abspath(path)
    os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
    fig.savefig(absolute_path, dpi=160)
    plt.close(fig)
    return absolute_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default=None, help="path to a MarketSnapshot JSON")
    parser.add_argument("--budget", type=float, default=5e6, help="CVaR99 bad-debt budget (USD)")
    parser.add_argument("--min-target-share", type=float, default=0.5)
    parser.add_argument("--n-scenarios", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--manifest",
        default=None,
        metavar="PATH",
        help="write a JSON reproducibility manifest with results and parameters",
    )
    parser.add_argument(
        "--figure",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help=(
            "write the empirical depth figure; defaults to "
            "docs/assets/<chain>_<asset>_depth.png"
        ),
    )
    parser.add_argument(
        "--ordered",
        action="store_true",
        help="clear the liquidation queue sequentially in bonus-priority order",
    )
    args = parser.parse_args()

    snapshot_path = os.path.abspath(args.snapshot or default_snapshot_path())
    snapshot = load_snapshot(snapshot_path)
    reserve = snapshot.reserve
    date = dt.datetime.fromtimestamp(snapshot.timestamp, dt.timezone.utc).date()

    print(
        f"Aave V3 {reserve.symbol} market report ({snapshot.chain}) "
        f"| block {snapshot.block:,} ({date})"
    )
    if snapshot.scan_blocks:
        chain = CHAINS.get(snapshot.chain)
        block_time = chain.block_time_s if chain else 12.0
        days = snapshot.scan_blocks * block_time / 86_400
        print(
            f"borrower sample: Borrow events over the last {snapshot.scan_blocks:,} blocks "
            f"(~{days:.0f} days); dormant borrowers outside that window are not sampled"
        )
    elif snapshot.notes:
        print(f"borrower sample: {snapshot.notes}")
    print(
        f"on-chain params: LT {reserve.liquidation_threshold:.2%} | LTV {reserve.ltv:.2%} "
        f"| bonus {reserve.liquidation_bonus:.2%} | oracle {_fmt(reserve.price_usd)}"
    )
    if snapshot.stress is not None:
        s = snapshot.stress
        dof = f"{s.t_dof:.1f}" if s.t_dof is not None else "n/a (thin tails)"
        print(
            f"calibration [{s.source}]: realized vol {s.annual_vol:.1%} "
            f"({s.lookback_days}d) | t-dof {dof}"
        )
        if s.peg_pass is not None:
            print(
                f"ARFC peg rule (>1% for >=2d): {'PASS' if s.peg_pass else 'FAIL'} "
                f"| worst deviation {s.peg_worst_deviation:.2%} "
                f"| longest breach {s.peg_max_run_days:.0f}d"
            )
    if snapshot.depth is not None:
        d = snapshot.depth
        if d.points:
            largest_quote = max(d.points, key=lambda point: point[0])
            print(
                f"depth [{d.source} {d.pair}]: empirical ladder to "
                f"{_fmt(largest_quote[0])} at {largest_quote[1]:.2%} slippage"
            )
        else:
            print(
                f"depth [{d.source} {d.pair}]: fitted curve at "
                f"{_fmt(d.ref_notional_usd)} gives {d.ref_slippage:.2%} slippage"
            )

    print("\nSupply side")
    print(f"  supply cap : {reserve.supply_cap_tokens:>14,.0f} {reserve.symbol}  {_fmt(reserve.supply_cap_usd)}")
    used = reserve.total_supplied_tokens / reserve.supply_cap_tokens if reserve.supply_cap_tokens else float("nan")
    print(f"  supplied   : {reserve.total_supplied_tokens:>14,.0f} {reserve.symbol}  {_fmt(reserve.supplied_usd)}  ({used:.0%} of cap)")

    dominant = [
        a for a in snapshot.accounts
        if a.collateral_usd > 0 and a.target_share >= args.min_target_share and a.debt_usd >= 10_000
    ]
    loopers = [a for a in dominant if a.eth_debt_share > 0.5]
    print(
        f"\n{reserve.symbol}-dominant accounts (share >= {args.min_target_share:.0%}): "
        f"{len(dominant)} | debt {_fmt(sum(a.debt_usd for a in dominant))}"
    )
    print(
        f"  of which ETH-debt loopers (debt falls with collateral; excluded from USD shock): "
        f"{len(loopers)} accounts, {_fmt(sum(a.debt_usd for a in loopers))} debt"
    )

    book = build_real_book(snapshot, min_target_share=args.min_target_share)
    print(
        f"USD-debt book entering the engine: {book.debt_usd.size} accounts "
        f"| debt {_fmt(book.total_debt)} "
        f"| collateral {_fmt(float(book.collateral_value(reserve.price_usd).sum()))} "
        f"| median HF {sorted(book.hf0)[book.hf0.size // 2]:.2f}"
    )

    config = scenario_config_from_snapshot(snapshot)
    config.sim = SimConfig(n_scenarios=args.n_scenarios, seed=args.seed, chunk_size=2_000)
    if args.ordered:
        config.risk = dataclasses.replace(config.risk, ordered_queue=True)
        print("queue model: ordered (sequential clearing in bonus-priority order)")
    engine = RiskEngine(config)

    base = engine.run(book=book)
    print("\nTail risk at observed book exposure")
    _print_tail_diagnostics(base)

    # The effective single-asset mapping attributes each account's whole
    # collateral to the target asset; show how much the conclusion depends
    # on how strictly the book is filtered to target-dominated accounts.
    print("\nSensitivity to the target-share filter (single-asset mapping)")
    shares = sorted({args.min_target_share, 0.7, 0.9})
    share_sensitivity = []
    for share in shares:
        try:
            b = build_real_book(snapshot, min_target_share=share)
        except ValueError:
            print(f"  share >= {share:.0%}: no accounts pass")
            share_sensitivity.append(
                {"min_target_share": share, "accounts": 0, "metrics": None}
            )
            continue
        r = engine.run(book=b)
        share_sensitivity.append(
            {
                "min_target_share": share,
                "accounts": int(b.debt_usd.size),
                "debt_usd": b.total_debt,
                "metrics": r.diagnostics(),
            }
        )
        print(
            f"  share >= {share:.0%}: {b.debt_usd.size:>3} accounts "
            f"| debt {_fmt(b.total_debt):>9} | CVaR99 {_fmt(r.cvar):>9} "
            f"| P(bad debt) {r.prob_bad_debt:.2%}"
        )

    combined = None
    try:
        full_book = build_real_book(
            snapshot, min_target_share=args.min_target_share, model_eth_debt=True
        )
    except ValueError:
        full_book = None
    if full_book is not None and full_book.eth_debt_usd is not None:
        eth_total = float(full_book.eth_debt_usd.sum())
        combined = engine.run(book=full_book)
        print("\nCombined book with ETH-denominated debt modeled")
        print(
            f"  full book: {full_book.debt_usd.size} accounts "
            f"| debt {_fmt(full_book.total_debt)} "
            f"(of which ETH-denominated {_fmt(eth_total)})"
        )
        print(
            f"  P(bad debt) {combined.prob_bad_debt:.2%} | VaR99 {_fmt(combined.var)} "
            f"| CVaR99 {_fmt(combined.cvar)} | positive draws "
            f"{combined.positive_loss_count:,}/{combined.bad_debt.size:,} "
            f"(USD-debt book alone: {_fmt(base.cvar)})"
        )
        print(
            "  note: ETH-denominated debt falls with the scenario ETH price, so"
            " loopers are stressed by the peg/exchange-rate and depth terms"
            " rather than the USD price level."
        )

    rec = engine.recommend_cap(
        budget_usd=args.budget,
        cap_min=book.total_debt * 0.1,
        cap_max=book.total_debt * 3.0,
        n_grid=18,
        book=book,
    )
    safe = rec["recommended_cap"]
    print(f"\nModel-safe debt exposure (CVaR99 budget {_fmt(args.budget)})")
    print(
        "  scope: this cap recommendation applies to the USD-debt book; the"
        " combined view above is diagnostic."
    )
    print(f"  observed book debt : {_fmt(book.total_debt)}")
    print(f"  model safe exposure: {_fmt(safe)}")
    if safe == safe:  # not NaN
        gap = safe - book.total_debt
        print(f"  headroom           : {'+' if gap >= 0 else ''}{_fmt(gap)} ({gap / book.total_debt:+.0%})")
    else:
        print("  headroom           : n/a (budget breached across the whole sweep)")
    implied = reserve.supply_cap_usd * reserve.ltv
    print(f"  for reference, supply cap x LTV allows up to {_fmt(implied)} debt against {reserve.symbol}")

    clearance = None
    if snapshot.depth is None:
        print("\nARFC clearance test skipped: snapshot has no depth calibration")
    else:
        clearance = arfc_clearance_test(
            snapshot, min_target_share=args.min_target_share
        )
        print("\nARFC clearance test (largest borrower within liquidation bonus)")
        print(
            f"  liquidator break-even slippage : "
            f"{clearance.breakeven_slippage:.2%}"
        )
        print(
            f"  largest borrower sale          : "
            f"{_fmt(clearance.largest_borrower_usd)}"
        )
        print(f"  largest borrower account       : {clearance.largest_account}")
        print(
            "  target collateral / debt       : "
            f"{_fmt(clearance.largest_target_collateral_usd)} / "
            f"{_fmt(clearance.largest_debt_usd)}"
        )
        print(
            f"  ETH-denominated debt share     : "
            f"{clearance.largest_eth_debt_share:.2%}"
        )
        print(f"  top-5 borrower sales           : {_fmt(clearance.top5_borrowers_usd)}")
        if clearance.slippage_quiet_is_lower_bound:
            print(
                f"  caution: the sale exceeds the quote ladder top "
                f"({_fmt(clearance.max_quoted_usd)}); the displayed slippage is "
                "the last observed value and only a lower bound"
            )
        quiet_prefix = ">= " if clearance.slippage_quiet_is_lower_bound else ""
        stressed_prefix = ">= " if clearance.slippage_stressed_is_lower_bound else ""
        print(
            f"  quiet depth    : slippage {quiet_prefix}{clearance.slippage_quiet:.2%} "
            f"| max clearable {_fmt(clearance.max_clearable_usd_quiet)} "
            f"-> {'PASS' if clearance.passes_quiet else 'FAIL'}"
        )
        print(
            f"  stressed depth ({clearance.depth_haircut_stressed:.0%} haircut)"
            f" : slippage {stressed_prefix}{clearance.slippage_stressed:.2%} "
            f"| max clearable {_fmt(clearance.max_clearable_usd_stressed)} "
            f"-> {'PASS' if clearance.passes_stressed else 'FAIL'}"
        )
        print(
            "  note: this measures instant routed on-chain exits only and excludes"
            " CEX or OTC liquidity."
        )
        if reserve.symbol.lower() not in {"weth", "wbtc", "usdc", "usdt"}:
            print(
                "  note: LST liquidators can also exit via a redemption queue over"
                " days, which this strict reading of the ARFC requirement does not"
                " credit."
            )
        print(
            "  note: unlike the USD-shock book, this test includes ETH-debt"
            " loopers: their collateral still sells on this asset's depth curve"
            " when liquidated, so clearance is a pure market-depth question."
        )
        if args.figure is not None:
            print(f"\nwrote {_write_depth_figure(snapshot, args.figure, clearance)}")

    if args.manifest:
        manifest_path = _write_manifest(
            args.manifest,
            snapshot_path,
            snapshot,
            config,
            args,
            book,
            base,
            full_book,
            combined,
            share_sensitivity,
            rec,
            clearance,
        )
        print(f"\nwrote {manifest_path}")


if __name__ == "__main__":
    main()
