"""Reproduce the HINC/HYG RWA drawdown and warehouse sensitivity."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

from .rwa_drawdown import (
    conditional_lookback_sweep,
    load_adjusted_close_csv,
    maximum_supported_recovery_loss,
    minimum_economic_bonus,
    monthly_return_windows,
    stress_shape_bracket,
    worst_forward_after_drawdown,
    worst_return_window,
)


_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_DATA = (
    _PACKAGE_DIR
    / "data"
    / "rwa"
    / "hyg_adjusted_close_2016-07-01_2026-07-15.csv"
)
_DEFAULT_METADATA = _DEFAULT_DATA.with_suffix(".metadata.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: str, payload: dict) -> str:
    absolute = os.path.abspath(path)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with open(absolute, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return absolute


def _window_payload(window) -> dict:
    return {
        "start_date": window.start_date.isoformat(),
        "end_date": window.end_date.isoformat(),
        "sessions": window.sessions,
        "calendar_days": window.calendar_days,
        "return": window.return_value,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="RWA four-session drawdown and liquidator bonus sensitivity"
    )
    parser.add_argument("--data", default=str(_DEFAULT_DATA))
    parser.add_argument("--metadata", default=str(_DEFAULT_METADATA))
    parser.add_argument("--sessions", type=int, default=4)
    parser.add_argument("--lookback-min", type=int, default=20)
    parser.add_argument("--lookback-max", type=int, default=250)
    parser.add_argument(
        "--drawdown-thresholds", type=float, nargs="+", default=(0.05, 0.10)
    )
    parser.add_argument("--reported-blend-worst-month-loss", type=float, default=0.1825)
    parser.add_argument("--funding-annual-rate", type=float, default=0.10)
    parser.add_argument("--hurdle-annual-rate", type=float, default=0.10)
    parser.add_argument("--generic-calendar-days", type=float, default=4.0)
    parser.add_argument("--manifest")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    data_path = Path(args.data).resolve()
    metadata_path = Path(args.metadata).resolve()
    observations = load_adjusted_close_csv(data_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    worst = worst_return_window(observations, args.sessions)
    monthly = sorted(monthly_return_windows(observations), key=lambda item: item.return_value)
    worst_month, second_worst_month = monthly[:2]

    conditional = {}
    stability = {}
    for threshold in args.drawdown_thresholds:
        representative = worst_forward_after_drawdown(
            observations,
            args.sessions,
            threshold,
            60,
        )
        sweep = conditional_lookback_sweep(
            observations,
            args.sessions,
            threshold,
            args.lookback_min,
            args.lookback_max,
        )
        window_keys = {
            (
                item.window.start_date,
                item.window.end_date,
                round(item.window.return_value, 14),
            )
            for item in sweep
        }
        conditional[threshold] = representative
        stability[threshold] = {
            "stable": len(window_keys) == 1,
            "unique_window_count": len(window_keys),
            "prior_drawdown_min": min(item.prior_drawdown for item in sweep),
            "prior_drawdown_max": max(item.prior_drawdown for item in sweep),
        }

    stress_concentration_ratio, scaled_loss = stress_shape_bracket(
        abs(worst.return_value),
        abs(worst_month.return_value),
        args.reported_blend_worst_month_loss,
    )
    proxy_bonus_four_days = minimum_economic_bonus(
        abs(worst.return_value),
        args.generic_calendar_days,
        args.funding_annual_rate,
        args.hurdle_annual_rate,
    )
    proxy_bonus_elapsed = minimum_economic_bonus(
        abs(worst.return_value),
        worst.calendar_days,
        args.funding_annual_rate,
        args.hurdle_annual_rate,
    )
    bonus_rows = [
        {
            "label": "3% loss sensitivity",
            "loss": 0.03,
            "calendar_days": args.generic_calendar_days,
            "minimum_bonus": minimum_economic_bonus(
                0.03,
                args.generic_calendar_days,
                args.funding_annual_rate,
                args.hurdle_annual_rate,
            ),
        },
        {
            "label": "5% loss sensitivity",
            "loss": 0.05,
            "calendar_days": args.generic_calendar_days,
            "minimum_bonus": minimum_economic_bonus(
                0.05,
                args.generic_calendar_days,
                args.funding_annual_rate,
                args.hurdle_annual_rate,
            ),
        },
        {
            "label": "HYG worst four-session window",
            "loss": abs(worst.return_value),
            "calendar_days": worst.calendar_days,
            "minimum_bonus": proxy_bonus_elapsed,
        },
        {
            "label": "stress-shape bracket",
            "loss": scaled_loss,
            "calendar_days": worst.calendar_days,
            "minimum_bonus": minimum_economic_bonus(
                scaled_loss,
                worst.calendar_days,
                args.funding_annual_rate,
                args.hurdle_annual_rate,
            ),
        },
    ]
    compensation_rows = [
        {
            "economic_compensation_rate": rate,
            "calendar_days": worst.calendar_days,
            "maximum_supported_nav_loss": maximum_supported_recovery_loss(
                rate,
                worst.calendar_days,
                args.funding_annual_rate,
                args.hurdle_annual_rate,
            ),
        }
        for rate in (0.03, 0.05)
    ]

    print("RWA drawdown and permissioned-liquidator stress proxy")
    print(
        f"  sample                     : {observations[0].date} to "
        f"{observations[-1].date} | {len(observations):,} observations"
    )
    print(
        f"  source                     : {metadata['provider']} "
        f"{metadata['symbol']} adjusted close | retrieved "
        f"{metadata['retrieved_at_utc'][:10]}"
    )
    print(
        f"  return definition          : close-to-close over {args.sessions} "
        f"sessions ({args.sessions + 1} observations)"
    )
    print("\nWorst empirical windows")
    print(
        f"  unconditional              : {worst.start_date} to {worst.end_date} | "
        f"{worst.return_value:.2%} | {worst.calendar_days} calendar days"
    )
    for threshold in args.drawdown_thresholds:
        item = conditional[threshold]
        stable = stability[threshold]
        label = f"after {threshold:.0%} drawdown"
        print(
            f"  {label:<28}: "
            f"{item.window.start_date} to {item.window.end_date} | "
            f"prior {item.prior_drawdown:.2%} | forward "
            f"{item.window.return_value:.2%} | lookbacks "
            f"{args.lookback_min}-{args.lookback_max} "
            f"{'stable' if stable['stable'] else 'not stable'}"
        )

    print("\nWorst calendar months")
    print(
        f"  1                          : {worst_month.end_date:%Y-%m} | "
        f"{worst_month.return_value:.2%}"
    )
    print(
        f"  2                          : {second_worst_month.end_date:%Y-%m} | "
        f"{second_worst_month.return_value:.2%}"
    )
    print("\nStress-shape bracket (heuristic, not a HINC estimate)")
    print(
        f"  HYG concentration ratio    : {abs(worst.return_value):.2%} / "
        f"{abs(worst_month.return_value):.2%} = "
        f"{stress_concentration_ratio:.2f}x"
    )
    print(
        f"  disclosed blend worst month: "
        f"{args.reported_blend_worst_month_loss:.2%}"
    )
    print(f"  four-session bracket       : {scaled_loss:.2%}")

    print(
        "\nImplied NAV-loss ceiling "
        "(only if 3%-5% is economic compensation)"
    )
    print(" compensation | cal. days | maximum NAV loss")
    print("----------------------------------------------")
    for row in compensation_rows:
        print(
            f" {row['economic_compensation_rate']:>11.2%} | "
            f"{row['calendar_days']:>9.0f} | "
            f"{row['maximum_supported_nav_loss']:>16.2%}"
        )

    print("\nMinimum economic bonus")
    print(" scenario                         | NAV loss | cal. days | min bonus")
    print("-------------------------------------------------------------------")
    for row in bonus_rows:
        print(
            f" {row['label']:<32} | {row['loss']:>8.2%} | "
            f"{row['calendar_days']:>9.0f} | {row['minimum_bonus']:>9.2%}"
        )
    weekend_bps = 10_000.0 * (proxy_bonus_elapsed - proxy_bonus_four_days)
    print(
        f"  note: charging the observed March window for {worst.calendar_days} "
        f"calendar days instead of four adds {weekend_bps:.0f} bps."
    )
    print(
        "  note: gross stablecoin financing equals the debt tranche repaid at "
        "time zero; the bonus rows measure loss compensation, not financing."
    )
    print(
        "  note: the bracket transfers HYG's within-month stress concentration "
        "to the disclosed blend worst month and is not a HINC estimate."
    )

    if args.manifest:
        payload = {
            "schema_version": 1,
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "command": [
                "python",
                "-m",
                "aave_risk_engine.run_rwa_drawdown_stress",
                *(argument.replace("\\", "/") for argument in sys.argv[1:]),
            ],
            "dataset": {
                "file": os.path.relpath(data_path, _PACKAGE_DIR).replace("\\", "/"),
                "sha256": _sha256(data_path),
                "metadata_file": os.path.relpath(
                    metadata_path, _PACKAGE_DIR
                ).replace("\\", "/"),
                "metadata_sha256": _sha256(metadata_path),
                "provider": metadata["provider"],
                "symbol": metadata["symbol"],
                "field": metadata["field"],
                "retrieved_at_utc": metadata["retrieved_at_utc"],
                "start_date": observations[0].date.isoformat(),
                "end_date": observations[-1].date.isoformat(),
                "observations": len(observations),
            },
            "definitions": {
                "sessions": args.sessions,
                "observations_per_return": args.sessions + 1,
                "return": "adjusted_close[t+sessions] / adjusted_close[t] - 1",
                "conditional_start": (
                    "start adjusted close is at or below the selected drawdown "
                    "from the inclusive rolling maximum"
                ),
                "monthly_return": "consecutive calendar month-end observations",
            },
            "assumptions": {
                "conditional_drawdown_thresholds": args.drawdown_thresholds,
                "lookback_sessions": [args.lookback_min, args.lookback_max],
                "reported_hinc_blend_worst_month_loss": (
                    args.reported_blend_worst_month_loss
                ),
                "funding_annual_rate": args.funding_annual_rate,
                "hurdle_annual_rate": args.hurdle_annual_rate,
                "generic_calendar_days": args.generic_calendar_days,
                "gross_debt_repayment_normalization_usd": 100_000_000.0,
            },
            "results": {
                "unconditional_worst_window": _window_payload(worst),
                "conditional_windows": {
                    f"{threshold:.6f}": {
                        "representative_lookback_sessions": 60,
                        "prior_drawdown": conditional[threshold].prior_drawdown,
                        "window": _window_payload(conditional[threshold].window),
                        "lookback_sweep": stability[threshold],
                    }
                    for threshold in args.drawdown_thresholds
                },
                "worst_month": _window_payload(worst_month),
                "second_worst_month": _window_payload(second_worst_month),
                "stress_shape_bracket": {
                    "hyg_four_session_to_worst_month_ratio": (
                        stress_concentration_ratio
                    ),
                    "disclosed_blend_worst_month_loss": (
                        args.reported_blend_worst_month_loss
                    ),
                    "four_session_loss": scaled_loss,
                    "is_estimate": False,
                },
                "economic_compensation_loss_ceilings": compensation_rows,
                "minimum_bonus_scenarios": bonus_rows,
                "four_calendar_day_proxy_bonus": proxy_bonus_four_days,
                "observed_elapsed_proxy_bonus": proxy_bonus_elapsed,
                "weekend_increment_bps": weekend_bps,
            },
            "missing_hinc_inputs": [
                "dated daily 70/30 blend levels or returns",
                "LTV and liquidation threshold",
                "liquidation bonus and close-factor rule",
                "definition and USD amount of the 3-5% backstop",
                "redemption cut-off, throughput, fee, gating, and suspension terms",
                "distribution of debt and health factors across positions",
            ],
            "notes": [
                "HYG is a market-price proxy, not HINC NAV or the exact blend.",
                "The stress-shape bracket is heuristic and is not a HINC estimate.",
                "Permissioning makes committed liquidator financing exogenous.",
                "Daily NAV updates can trigger multiple positions simultaneously.",
                "Gross financing, loss buffer, and minimum bonus are distinct quantities.",
            ],
        }
        print(f"\nwrote {_write_json(args.manifest, payload)}")


if __name__ == "__main__":
    main()
