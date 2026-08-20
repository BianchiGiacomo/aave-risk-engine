"""Liquidator warehouse economics for the largest target-dominant borrower.

Usage:
    python -m aave_risk_engine.run_liquidator_balance_sheet [--snapshot path]
        [--redemption-usd-per-day 25000000]
        [--stressed-redemption-usd-per-day 12500000] [--figure [path]]

Every funding, hedge, redemption, and refill input is an explicit sensitivity.
The output is not a measurement of available liquidator capital or live Lido
withdrawal capacity.
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
import numpy as np

from . import plotting
from .data import arfc_clearance_test, default_snapshot_path, load_snapshot
from .liquidator_balance_sheet import (
    LiquidatorAssumptions,
    break_even_basis_loss,
    minimum_liquidation_bonus,
    simulate_liquidator_balance_sheet,
)
from .slippage import calibrate_liquidity, max_notional_within
from .time_to_exit import ExitAssumptions


def _fmt(value: float) -> str:
    sign = "-" if value < 0.0 else ""
    absolute = abs(value)
    for unit, divisor in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if absolute >= divisor:
            return f"{sign}${absolute / divisor:,.2f}{unit}"
    return f"{sign}${absolute:,.0f}"


def _time_label(hours: float | None) -> str:
    if hours is None:
        return "not clearable"
    if hours < 24.0:
        return f"{hours:.1f}h"
    return f"{hours / 24.0:.2f}d"


def _parse_losses(value: str) -> tuple[float, ...]:
    try:
        losses = tuple(sorted(set(float(item.strip()) for item in value.split(","))))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "basis losses must be comma-separated fractions"
        ) from exc
    if not losses or losses[0] < 0.0 or losses[-1] >= 1.0:
        raise argparse.ArgumentTypeError("basis losses must be in [0, 1)")
    return losses


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


def _instant_capacity(snapshot, execution_loss: float) -> float:
    if snapshot.depth is None:
        raise ValueError("snapshot has no depth calibration")
    if snapshot.depth.points and len(snapshot.depth.points) >= 2:
        return max_notional_within(snapshot.depth.points, execution_loss)
    liquidity = calibrate_liquidity(
        snapshot.reserve.price_usd,
        snapshot.depth.ref_notional_usd,
        snapshot.depth.ref_slippage,
    )
    return float(
        np.sqrt(snapshot.reserve.price_usd)
        * liquidity
        * execution_loss
        / (1.0 - execution_loss)
    )


def _result_payload(result) -> dict:
    fields = (
        "cleared",
        "time_to_clear_hours",
        "weighted_average_exit_hours",
        "dex_exit_usd",
        "redemption_exit_usd",
        "unresolved_collateral_usd",
        "realized_recovery_usd",
        "gross_bonus_usd",
        "funding_cost_usd",
        "hedge_entry_cost_usd",
        "hedge_carry_cost_usd",
        "fixed_cost_usd",
        "peak_capital_usd",
        "capital_days_usd",
        "accounting_profit_usd",
        "hurdle_charge_usd",
        "economic_profit_usd",
        "accounting_roi",
        "economic_roi",
        "economic_clearance_pass",
    )
    return {name: getattr(result, name) for name in fields}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default=None)
    parser.add_argument("--min-target-share", type=float, default=0.5)
    parser.add_argument("--stress-depth-haircut", type=float, default=0.5)
    parser.add_argument("--quiet-refill-hours", type=float, default=6.0)
    parser.add_argument("--stressed-refill-hours", type=float, default=24.0)
    parser.add_argument("--redemption-delay-hours", type=float, default=24.0)
    parser.add_argument("--redemption-usd-per-day", type=float, default=0.0)
    parser.add_argument(
        "--stressed-redemption-usd-per-day",
        type=float,
        default=None,
        help="defaults to --redemption-usd-per-day",
    )
    parser.add_argument("--funding-annual-rate", type=float, default=0.10)
    parser.add_argument("--hurdle-annual-rate", type=float, default=0.10)
    parser.add_argument("--hedge-entry-cost", type=float, default=0.001)
    parser.add_argument("--hedge-carry-annual-rate", type=float, default=0.02)
    parser.add_argument("--dex-execution-loss", type=float, default=0.01)
    parser.add_argument("--redemption-loss", type=float, default=0.0)
    parser.add_argument("--fixed-cost-usd", type=float, default=0.0)
    parser.add_argument("--step-hours", type=float, default=1.0)
    parser.add_argument("--max-horizon-days", type=float, default=365.0)
    parser.add_argument(
        "--basis-losses",
        type=_parse_losses,
        default=(0.0, 0.02, 0.04, 0.06, 0.08, 0.10),
    )
    parser.add_argument(
        "--figure",
        nargs="?",
        const="docs/assets/wsteth_liquidator_balance_sheet.png",
        default=None,
    )
    parser.add_argument("--manifest", default=None)
    args = parser.parse_args()

    nonnegative = (
        "min_target_share",
        "stress_depth_haircut",
        "redemption_delay_hours",
        "redemption_usd_per_day",
        "funding_annual_rate",
        "hurdle_annual_rate",
        "hedge_entry_cost",
        "hedge_carry_annual_rate",
        "dex_execution_loss",
        "redemption_loss",
        "fixed_cost_usd",
    )
    if any(getattr(args, name) < 0.0 for name in nonnegative):
        parser.error("share, haircut, throughput, costs, and rates must be non-negative")
    if (
        args.stressed_redemption_usd_per_day is not None
        and args.stressed_redemption_usd_per_day < 0.0
    ):
        parser.error("stressed redemption throughput must be non-negative")
    if args.min_target_share > 1.0:
        parser.error("--min-target-share must be at most 1")
    if args.stress_depth_haircut >= 1.0:
        parser.error("--stress-depth-haircut must be below 1")
    if args.dex_execution_loss <= 0.0 or args.dex_execution_loss >= 1.0:
        parser.error("--dex-execution-loss must be in (0, 1)")
    if args.redemption_loss >= 1.0:
        parser.error("--redemption-loss must be below 1")
    if args.quiet_refill_hours <= 0.0 or args.stressed_refill_hours <= 0.0:
        parser.error("refill hours must be positive")
    if args.step_hours <= 0.0 or args.max_horizon_days <= 0.0:
        parser.error("step and horizon must be positive")

    snapshot_path = os.path.abspath(args.snapshot or default_snapshot_path())
    snapshot = load_snapshot(snapshot_path)
    clearance = arfc_clearance_test(
        snapshot,
        stressed_haircut=args.stress_depth_haircut,
        min_target_share=args.min_target_share,
    )
    sale = clearance.largest_borrower_usd
    bonus = snapshot.reserve.liquidation_bonus
    debt = sale / (1.0 + bonus)
    quiet_capacity = _instant_capacity(snapshot, args.dex_execution_loss)
    stressed_capacity = quiet_capacity * (1.0 - args.stress_depth_haircut)
    stressed_redemption_usd_per_day = (
        args.redemption_usd_per_day
        if args.stressed_redemption_usd_per_day is None
        else args.stressed_redemption_usd_per_day
    )
    regimes = {
        "quiet DEX": ExitAssumptions(
            quiet_capacity,
            args.quiet_refill_hours,
            0.0,
            args.redemption_delay_hours,
        ),
        "stressed DEX": ExitAssumptions(
            stressed_capacity,
            args.stressed_refill_hours,
            0.0,
            args.redemption_delay_hours,
        ),
    }
    redemption_enabled = (
        args.redemption_usd_per_day > 0.0
        or stressed_redemption_usd_per_day > 0.0
    )
    if args.redemption_usd_per_day > 0.0:
        regimes["quiet + redemption"] = ExitAssumptions(
            quiet_capacity,
            args.quiet_refill_hours,
            args.redemption_usd_per_day,
            args.redemption_delay_hours,
        )
    if stressed_redemption_usd_per_day > 0.0:
        regimes["stressed + redemption"] = ExitAssumptions(
            stressed_capacity,
            args.stressed_refill_hours,
            stressed_redemption_usd_per_day,
            args.redemption_delay_hours,
        )
    common = LiquidatorAssumptions(
        funding_annual_rate=args.funding_annual_rate,
        hurdle_annual_rate=args.hurdle_annual_rate,
        hedge_entry_cost=args.hedge_entry_cost,
        hedge_carry_annual_rate=args.hedge_carry_annual_rate,
        dex_execution_loss=args.dex_execution_loss,
        redemption_loss=args.redemption_loss,
        fixed_cost_usd=args.fixed_cost_usd,
        step_hours=args.step_hours,
        max_horizon_hours=24.0 * args.max_horizon_days,
    )

    sweep = {}
    for basis_loss in args.basis_losses:
        assumptions = dataclasses.replace(common, basis_loss=basis_loss)
        sweep[basis_loss] = {}
        for label, exit_assumptions in regimes.items():
            result = simulate_liquidator_balance_sheet(
                debt, sale, exit_assumptions, assumptions
            )
            minimum_bonus = minimum_liquidation_bonus(
                debt, exit_assumptions, assumptions
            )
            sweep[basis_loss][label] = {
                "result": result,
                "minimum_bonus": minimum_bonus,
            }

    basis_thresholds = {
        label: break_even_basis_loss(debt, sale, regime, common)
        for label, regime in regimes.items()
    }

    print(
        f"Liquidator balance sheet | {snapshot.reserve.symbol} "
        f"({snapshot.chain}) block {snapshot.block:,}"
    )
    print(f"largest borrower account : {clearance.largest_account}")
    print(f"debt repaid upfront      : {_fmt(debt)}")
    print(f"collateral seized        : {_fmt(sale)}")
    print(f"gross liquidation bonus  : {_fmt(sale - debt)} ({bonus:.2%})")
    print(f"warehouse base capital  : {_fmt(debt)} before costs")
    print(
        f"strict instant ARFC      : "
        f"{'PASS' if clearance.passes_quiet else 'FAIL'}"
    )
    print("\nIllustrative warehouse assumptions")
    print(f"  DEX execution loss ceiling : {args.dex_execution_loss:.2%}")
    print(
        f"  instant DEX capacity       : quiet {_fmt(quiet_capacity)} | "
        f"stressed {_fmt(stressed_capacity)}"
    )
    print(
        f"  DEX equivalent refill      : quiet {args.quiet_refill_hours:g}h | "
        f"stressed {args.stressed_refill_hours:g}h"
    )
    if redemption_enabled:
        print(
            f"  redemption benchmark       : quiet "
            f"{_fmt(args.redemption_usd_per_day)}/day | stressed "
            f"{_fmt(stressed_redemption_usd_per_day)}/day "
            f"after {args.redemption_delay_hours:g}h"
        )
    else:
        print(
            "  redemption benchmark       : disabled "
            "(use --redemption-usd-per-day)"
        )
    print(
        f"  funding / hurdle annual    : {args.funding_annual_rate:.1%} / "
        f"{args.hurdle_annual_rate:.1%} "
        f"({args.funding_annual_rate + args.hurdle_annual_rate:.1%} "
        "combined economic charge)"
    )
    print(
        f"  hedge entry / annual carry : {args.hedge_entry_cost:.2%} / "
        f"{args.hedge_carry_annual_rate:.1%}"
    )

    print("\nProfit after hurdle by residual basis loss")
    display_labels = {
        "quiet DEX": "quiet DEX",
        "stressed DEX": "stress DEX",
        "quiet + redemption": "quiet + red.",
        "stressed + redemption": "stress + red.",
    }
    widths = {
        "quiet DEX": 12,
        "stressed DEX": 12,
        "quiet + redemption": 12,
        "stressed + redemption": 13,
    }
    header = f"{'basis':>7} | " + " | ".join(
        f"{display_labels[label]:>{widths[label]}}" for label in regimes
    )
    print(header)
    print("-" * len(header))
    for basis_loss in args.basis_losses:
        def profit_label(value):
            return "not cleared" if value is None else _fmt(value)
        values = [
            sweep[basis_loss][label]["result"].economic_profit_usd
            for label in regimes
        ]
        print(
            f"{basis_loss:>6.1%} | "
            + " | ".join(
                f"{profit_label(value):>{widths[label]}}"
                for label, value in zip(regimes, values)
            )
        )

    print("\nCurrent-bonus residual-basis threshold")
    for label, value in basis_thresholds.items():
        rendered = "not profitable at zero basis loss" if value is None else f"{value:.2%}"
        print(f"  {label:<9}: {rendered}")

    reference_basis = min(args.basis_losses, key=lambda value: abs(value - 0.04))
    print(f"\nWarehouse balance sheet at {reference_basis:.1%} residual basis loss")
    for label in regimes:
        result = sweep[reference_basis][label]["result"]
        required = sweep[reference_basis][label]["minimum_bonus"]
        required_label = "n/a" if required is None else f"{required:.2%}"
        print(
            f"  {label:<23}: clear {_time_label(result.time_to_clear_hours)} | "
            f"average exit {_time_label(result.weighted_average_exit_hours)} | "
            f"peak {_fmt(result.peak_capital_usd)} | "
            f"economic profit {_fmt(result.economic_profit_usd or 0.0)} | "
            f"ROI {(result.economic_roi or 0.0):.2%} | min bonus {required_label}"
        )
    print(f"\nEconomic-clearance decision at {reference_basis:.1%} residual basis loss")
    for label in regimes:
        result = sweep[reference_basis][label]["result"]
        required = sweep[reference_basis][label]["minimum_bonus"]
        if required is None:
            decision = "FAIL | minimum bonus not found within search range"
        else:
            gap_bps = 10_000.0 * (required - bonus)
            verdict = "PASS" if result.economic_clearance_pass else "FAIL"
            decision = (
                f"{verdict} | current {bonus:.2%} | minimum {required:.2%} | "
                f"gap {gap_bps:+.0f} bps"
            )
        print(f"  {label:<23}: {decision}")
    print(
        "  note: ETH/USD is assumed hedged. The basis loss is residual "
        "wstETH/ETH or canonical-rate risk."
    )
    print(
        "  note: this is a full upfront warehouse strategy, not evidence "
        "of available liquidator capital or live redemption throughput."
    )
    print(
        "  note: capacity is used as soon as available. This is not a "
        "profit-maximizing route policy, and close factors can split repayment."
    )
    if (
        args.redemption_usd_per_day > 0.0
        and np.isclose(
            args.redemption_usd_per_day,
            stressed_redemption_usd_per_day,
        )
    ):
        print(
            "  note: identical redemption capacity can make the DEX-haircut "
            "path more profitable by routing less collateral through the "
            "costlier DEX; use --stressed-redemption-usd-per-day to stress it."
        )

    economic_profit = {
        label: [
            sweep[basis][label]["result"].economic_profit_usd
            for basis in args.basis_losses
        ]
        for label in regimes
    }
    required_bonus = {
        label: [
            (
                np.nan
                if sweep[basis][label]["minimum_bonus"] is None
                else sweep[basis][label]["minimum_bonus"]
            )
            for basis in args.basis_losses
        ]
        for label in regimes
    }
    if args.figure is not None:
        fig = plotting.plot_liquidator_balance_sheet_sensitivity(
            list(args.basis_losses),
            economic_profit,
            required_bonus,
            bonus,
            title=(
                f"{snapshot.chain.title()} {snapshot.reserve.symbol} "
                "liquidator warehouse sensitivity"
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
                "aave_risk_engine.run_liquidator_balance_sheet",
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
            "strategy": "full upfront warehouse; fastest available exit",
            "largest_borrower": {
                "account": clearance.largest_account,
                "debt_repaid_usd": debt,
                "seized_collateral_usd": sale,
                "strict_instant_pass": clearance.passes_quiet,
            },
            "liquidation_bonus": bonus,
            "common_assumptions": dataclasses.asdict(common),
            "redemption_throughput_usd_per_day": {
                "quiet": args.redemption_usd_per_day,
                "stressed": stressed_redemption_usd_per_day,
            },
            "regimes": {
                label: dataclasses.asdict(value) for label, value in regimes.items()
            },
            "basis_sweep": {
                f"{basis:.6f}": {
                    label: {
                        "result": _result_payload(values["result"]),
                        "minimum_bonus": values["minimum_bonus"],
                    }
                    for label, values in regimes_at_basis.items()
                }
                for basis, regimes_at_basis in sweep.items()
            },
            "break_even_basis_loss": basis_thresholds,
            "notes": [
                "All capacity and cost inputs are explicit sensitivities.",
                "ETH/USD is assumed hedged; basis loss is residual wstETH/ETH risk.",
                "The strategy repays the full selected debt at time zero.",
                "Available capacity is used immediately; routes are not optimized for profit.",
                "V3 close factors may split the selected repayment across transactions.",
                "Funding and hurdle rates both accrue on capital-days and add economically.",
                "Economic clearance does not change the strict instant ARFC verdict.",
            ],
        }
        print(f"\nwrote {_write_json(args.manifest, payload)}")


if __name__ == "__main__":
    main()
