"""Time-to-exit analysis for the largest target-dominant borrower.

Usage:
    python -m aave_risk_engine.run_time_to_exit [--snapshot path]
        [--redemption-usd-per-day 25000000] [--figure [path]]

The refill and redemption inputs are explicit sensitivities, not measurements
of live Lido queue throughput. The report also solves for the redemption rate
required to clear at each horizon.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import sys

import matplotlib.pyplot as plt

from . import plotting
from .data import arfc_clearance_test, default_snapshot_path, load_snapshot
from .time_to_exit import (
    DEFAULT_HORIZONS_HOURS,
    ExitAssumptions,
    build_exit_curve,
    required_redemption_usd_per_day,
)


def _fmt(value: float) -> str:
    for unit, divisor in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(value) >= divisor:
            return f"${value / divisor:,.2f}{unit}"
    return f"${value:,.0f}"


def _horizon_label(hours: float) -> str:
    if hours == 0.0:
        return "instant"
    if hours < 24.0:
        return f"{hours:g}h"
    return f"{hours / 24.0:g}d"


def _time_label(hours: float | None) -> str:
    if hours is None:
        return "not clearable"
    if hours < 24.0:
        return f"{hours:.1f}h"
    return f"{hours / 24.0:.2f}d"


def _parse_horizons(value: str) -> tuple[float, ...]:
    try:
        horizons = tuple(sorted(set(float(item.strip()) for item in value.split(","))))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("horizons must be comma-separated hours") from exc
    if not horizons or horizons[0] < 0.0:
        raise argparse.ArgumentTypeError("horizons must be non-negative")
    return horizons


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: str, payload: dict) -> str:
    absolute_path = os.path.abspath(path)
    os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
    with open(absolute_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, allow_nan=False)
        fh.write("\n")
    return absolute_path


def _curve_payload(curve) -> dict:
    return {
        "assumptions": dataclasses.asdict(curve.assumptions),
        "time_to_clear_hours": curve.time_to_clear_hours,
        "points": [dataclasses.asdict(point) for point in curve.points],
    }


def _series(curves: dict) -> dict[str, list[float]]:
    return {
        label: [point.total_capacity_usd for point in curve.points]
        for label, curve in curves.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default=None)
    parser.add_argument("--min-target-share", type=float, default=0.5)
    parser.add_argument("--stress-depth-haircut", type=float, default=0.5)
    parser.add_argument("--quiet-refill-hours", type=float, default=6.0)
    parser.add_argument("--stressed-refill-hours", type=float, default=24.0)
    parser.add_argument("--redemption-delay-hours", type=float, default=24.0)
    parser.add_argument("--redemption-usd-per-day", type=float, default=0.0)
    parser.add_argument("--stalled-drawdown", type=float, default=0.10)
    parser.add_argument(
        "--horizons-hours",
        type=_parse_horizons,
        default=DEFAULT_HORIZONS_HOURS,
        help="comma-separated horizon hours",
    )
    parser.add_argument(
        "--figure",
        nargs="?",
        const="docs/assets/wsteth_time_to_exit.png",
        default=None,
    )
    parser.add_argument("--manifest", default=None)
    args = parser.parse_args()

    numeric_nonnegative = (
        "min_target_share",
        "stress_depth_haircut",
        "redemption_delay_hours",
        "redemption_usd_per_day",
        "stalled_drawdown",
    )
    if any(getattr(args, name) < 0.0 for name in numeric_nonnegative):
        parser.error("share, haircut, delay, redemption, and drawdown must be non-negative")
    if args.min_target_share > 1.0:
        parser.error("--min-target-share must be at most 1")
    if args.stress_depth_haircut >= 1.0:
        parser.error("--stress-depth-haircut must be below 1")
    if args.stalled_drawdown > 1.0:
        parser.error("--stalled-drawdown must be at most 1")
    if args.quiet_refill_hours <= 0.0 or args.stressed_refill_hours <= 0.0:
        parser.error("refill hours must be positive")

    snapshot_path = os.path.abspath(args.snapshot or default_snapshot_path())
    snapshot = load_snapshot(snapshot_path)
    clearance = arfc_clearance_test(
        snapshot,
        stressed_haircut=args.stress_depth_haircut,
        min_target_share=args.min_target_share,
    )
    sale = clearance.largest_borrower_usd
    bonus = snapshot.reserve.liquidation_bonus

    def assumptions(instant, refill, redemption):
        return ExitAssumptions(
            instant_dex_capacity_usd=instant,
            dex_refill_hours=refill,
            redemption_capacity_usd_per_day=redemption,
            redemption_delay_hours=args.redemption_delay_hours,
            stalled_drawdown=args.stalled_drawdown,
        )

    curves = {
        "quiet DEX": build_exit_curve(
            sale,
            bonus,
            assumptions(clearance.max_clearable_usd_quiet, args.quiet_refill_hours, 0.0),
            args.horizons_hours,
        ),
        "stressed DEX": build_exit_curve(
            sale,
            bonus,
            assumptions(
                clearance.max_clearable_usd_stressed,
                args.stressed_refill_hours,
                0.0,
            ),
            args.horizons_hours,
        ),
        "quiet + redemption": build_exit_curve(
            sale,
            bonus,
            assumptions(
                clearance.max_clearable_usd_quiet,
                args.quiet_refill_hours,
                args.redemption_usd_per_day,
            ),
            args.horizons_hours,
        ),
        "stressed + redemption": build_exit_curve(
            sale,
            bonus,
            assumptions(
                clearance.max_clearable_usd_stressed,
                args.stressed_refill_hours,
                args.redemption_usd_per_day,
            ),
            args.horizons_hours,
        ),
    }
    redemption_enabled = args.redemption_usd_per_day > 0.0
    visible_curves = (
        curves
        if redemption_enabled
        else {
            label: curve
            for label, curve in curves.items()
            if "redemption" not in label
        }
    )

    print(
        f"Time-to-exit | {snapshot.reserve.symbol} ({snapshot.chain}) "
        f"block {snapshot.block:,}"
    )
    print(f"largest borrower account : {clearance.largest_account}")
    print(f"largest clearance sale   : {_fmt(sale)}")
    print(f"instant quiet capacity   : {_fmt(clearance.max_clearable_usd_quiet)}")
    print(
        f"strict instant ARFC      : "
        f"{'PASS' if clearance.passes_quiet else 'FAIL'}"
    )
    print("\nIllustrative sensitivity assumptions (not live Lido queue calibration)")
    print(f"  quiet DEX equivalent refill   : every {args.quiet_refill_hours:g}h")
    print(
        f"  stressed DEX                  : {args.stress_depth_haircut:.0%} depth "
        f"haircut, refill every {args.stressed_refill_hours:g}h"
    )
    if redemption_enabled:
        print(
            f"  primary redemption benchmark : "
            f"{_fmt(args.redemption_usd_per_day)}/day "
            f"after {args.redemption_delay_hours:g}h"
        )
    else:
        print(
            "  primary redemption benchmark : disabled "
            "(use --redemption-usd-per-day)"
        )
    print(f"  unresolved-tranche drawdown   : {args.stalled_drawdown:.0%}")

    print("\nCumulative liquidation capacity")
    labels = [_horizon_label(value) for value in args.horizons_hours]
    if redemption_enabled:
        print(
            f"{'horizon':>8} | {'quiet DEX':>12} | {'stress DEX':>12} | "
            f"{'quiet + red.':>12} | {'stress + red.':>13}"
        )
        print("-" * 72)
        for index, label in enumerate(labels):
            values = [
                curve.points[index].total_capacity_usd for curve in curves.values()
            ]
            print(
                f"{label:>8} | {_fmt(values[0]):>12} | "
                f"{_fmt(values[1]):>12} | {_fmt(values[2]):>12} | "
                f"{_fmt(values[3]):>13}"
            )
    else:
        print(f"{'horizon':>8} | {'quiet DEX':>12} | {'stress DEX':>12}")
        print("-" * 38)
        for index, label in enumerate(labels):
            quiet = curves["quiet DEX"].points[index].total_capacity_usd
            stressed = curves["stressed DEX"].points[index].total_capacity_usd
            print(f"{label:>8} | {_fmt(quiet):>12} | {_fmt(stressed):>12}")

    print("\nEstimated time to clear")
    for label, curve in visible_curves.items():
        print(f"  {label:<23}: {_time_label(curve.time_to_clear_hours)}")

    print("\nRequired primary-redemption throughput to clear by horizon")
    print(f"{'horizon':>8} | {'quiet DEX':>14} | {'stressed DEX':>14}")
    print("-" * 44)
    required = {}
    for horizon, label in zip(args.horizons_hours, labels):
        quiet_required = required_redemption_usd_per_day(
            sale,
            clearance.max_clearable_usd_quiet,
            args.quiet_refill_hours,
            horizon,
            args.redemption_delay_hours,
        )
        stressed_required = required_redemption_usd_per_day(
            sale,
            clearance.max_clearable_usd_stressed,
            args.stressed_refill_hours,
            horizon,
            args.redemption_delay_hours,
        )
        required[label] = {"quiet": quiet_required, "stressed": stressed_required}

        def required_label(value):
            return "not active" if value is None else f"{_fmt(value)}/day"

        print(
            f"{label:>8} | {required_label(quiet_required):>14} | "
            f"{required_label(stressed_required):>14}"
        )

    print("\nConditional stalled-tranche loss after the selected drawdown")
    loss_labels = (
        ("quiet + redemption", "stressed + redemption")
        if redemption_enabled
        else ("quiet DEX", "stressed DEX")
    )
    for label in loss_labels:
        curve = curves[label]
        final = curve.points[-1]
        print(
            f"  {label:<23} at {labels[-1]}: unresolved "
            f"{_fmt(final.unresolved_sale_usd)} | loss "
            f"{_fmt(final.conditional_bad_debt_usd)}"
        )
    print(
        "  note: this is a conditional mark on the unresolved liquidation "
        "tranche, not a forecast of whole-account bad debt."
    )

    if args.figure is not None:
        benchmark = (
            f"redemption {_fmt(args.redemption_usd_per_day)}/day after "
            f"{args.redemption_delay_hours:g}h"
            if args.redemption_usd_per_day > 0.0
            else "redemption disabled"
        )
        fig = plotting.plot_time_to_exit_capacity(
            labels,
            _series(visible_curves),
            sale,
            title=(
                f"{snapshot.chain.title()} {snapshot.reserve.symbol} time-to-exit\n"
                f"DEX refill {args.quiet_refill_hours:g}h quiet / "
                f"{args.stressed_refill_hours:g}h stressed; {benchmark}"
            ),
        )
        absolute_figure = os.path.abspath(args.figure)
        os.makedirs(os.path.dirname(absolute_figure), exist_ok=True)
        fig.savefig(absolute_figure, dpi=180)
        plt.close(fig)
        print(f"\nwrote {absolute_figure}")

    if args.manifest:
        payload = {
            "schema_version": 1,
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "command": [
                "python",
                "-m",
                "aave_risk_engine.run_time_to_exit",
                *(argument.replace("\\", "/") for argument in sys.argv[1:]),
            ],
            "snapshot": {
                "file": os.path.relpath(
                    snapshot_path, os.path.dirname(__file__)
                ).replace("\\", "/"),
                "sha256": _sha256(snapshot_path),
                "chain": snapshot.chain,
                "block": snapshot.block,
                "timestamp": snapshot.timestamp,
                "asset": snapshot.reserve.symbol,
                "borrower_discovery": (
                    dataclasses.asdict(snapshot.borrower_discovery)
                    if snapshot.borrower_discovery is not None
                    else None
                ),
            },
            "largest_borrower": {
                "account": clearance.largest_account,
                "sale_usd": sale,
                "debt_usd": clearance.largest_debt_usd,
                "target_collateral_usd": clearance.largest_target_collateral_usd,
            },
            "liquidation_bonus": bonus,
            "horizons_hours": list(args.horizons_hours),
            "strict_instant_pass": clearance.passes_quiet,
            "curves": {
                label: _curve_payload(curve)
                for label, curve in visible_curves.items()
            },
            "required_redemption_usd_per_day": required,
            "notes": [
                "Refill and redemption inputs are sensitivities, not live queue calibration.",
                "Conditional loss marks only the unresolved liquidation tranche.",
            ],
        }
        print(f"\nwrote {_write_json(args.manifest, payload)}")


if __name__ == "__main__":
    main()
