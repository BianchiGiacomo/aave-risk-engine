"""Tests for the committed RWA drawdown proxy analysis."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import numpy as np

from aave_risk_engine.rwa_drawdown import (
    PriceObservation,
    conditional_lookback_sweep,
    load_adjusted_close_csv,
    maximum_supported_recovery_loss,
    minimum_economic_bonus,
    monthly_return_windows,
    return_windows,
    stress_shape_bracket,
    worst_return_window,
)


_DATA = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "rwa"
    / "hyg_adjusted_close_2016-07-01_2026-07-15.csv"
)
_METADATA = _DATA.with_suffix(".metadata.json")
_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "manifests"
    / "hinc-hyg-proxy-2026-08-31.json"
)


def test_committed_hyg_proxy_is_pinned():
    observations = load_adjusted_close_csv(_DATA)
    assert len(observations) == 2522
    assert observations[0].date == dt.date(2016, 7, 1)
    assert observations[-1].date == dt.date(2026, 7, 15)
    assert np.isclose(observations[0].adjusted_close_usd, 49.537883758544922)
    assert np.isclose(observations[-1].adjusted_close_usd, 79.424400329589844)


def test_four_session_definition_uses_five_observations():
    observations = tuple(
        PriceObservation(dt.date(2026, 1, day), value)
        for day, value in enumerate((100.0, 99.0, 98.0, 97.0, 90.0), start=1)
    )
    windows = return_windows(observations, 4)
    assert len(windows) == 1
    assert windows[0].start_date == dt.date(2026, 1, 1)
    assert windows[0].end_date == dt.date(2026, 1, 5)
    assert np.isclose(windows[0].return_value, -0.10)


def test_worst_and_conditional_windows_are_lookback_stable():
    observations = load_adjusted_close_csv(_DATA)
    worst = worst_return_window(observations, 4)
    assert worst.start_date == dt.date(2020, 3, 13)
    assert worst.end_date == dt.date(2020, 3, 19)
    assert worst.calendar_days == 6
    assert np.isclose(worst.return_value, -0.1086821194, atol=1e-10)

    expected = {
        0.05: (dt.date(2020, 3, 13), dt.date(2020, 3, 19), -0.1086821194),
        0.10: (dt.date(2020, 3, 17), dt.date(2020, 3, 23), -0.1012309575),
    }
    for threshold, (start, end, value) in expected.items():
        sweep = conditional_lookback_sweep(
            observations, 4, threshold, 20, 250
        )
        assert len(sweep) == 231
        assert {item.window.start_date for item in sweep} == {start}
        assert {item.window.end_date for item in sweep} == {end}
        assert all(
            np.isclose(item.window.return_value, value, atol=1e-10)
            for item in sweep
        )


def test_monthly_losses_and_stress_shape_bracket_match_release():
    observations = load_adjusted_close_csv(_DATA)
    months = sorted(monthly_return_windows(observations), key=lambda item: item.return_value)
    assert months[0].end_date == dt.date(2020, 3, 31)
    assert np.isclose(months[0].return_value, -0.10028032, atol=1e-8)
    assert months[1].end_date == dt.date(2022, 6, 30)
    assert np.isclose(months[1].return_value, -0.07049902, atol=1e-8)

    ratio, scaled = stress_shape_bracket(
        0.1086821194,
        abs(months[0].return_value),
        0.1825,
    )
    assert np.isclose(ratio, 1.08378316, atol=1e-8)
    assert np.isclose(scaled, 0.19779042, atol=1e-8)


def test_minimum_bonus_charges_elapsed_calendar_time():
    four_day = minimum_economic_bonus(0.1086821194, 4.0, 0.10, 0.10)
    six_day = minimum_economic_bonus(0.1086821194, 6.0, 0.10, 0.10)
    scaled = minimum_economic_bonus(0.19779042, 6.0, 0.10, 0.10)
    assert np.isclose(four_day, 0.12439322, atol=1e-8)
    assert np.isclose(six_day, 0.12562274, atol=1e-8)
    assert np.isclose(10_000.0 * (six_day - four_day), 12.295, atol=0.01)
    assert np.isclose(scaled, 0.25065531, atol=1e-8)
    assert np.isclose(
        maximum_supported_recovery_loss(0.03, 6.0, 0.10, 0.10),
        0.02593430,
        atol=1e-8,
    )
    assert np.isclose(
        maximum_supported_recovery_loss(0.05, 6.0, 0.10, 0.10),
        0.04448793,
        atol=1e-8,
    )


def test_release_manifest_matches_committed_inputs_and_results():
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["dataset"]["sha256"] == hashlib.sha256(_DATA.read_bytes()).hexdigest()
    assert manifest["dataset"]["metadata_sha256"] == hashlib.sha256(
        _METADATA.read_bytes()
    ).hexdigest()
    assert manifest["dataset"]["observations"] == 2522
    assert np.isclose(
        manifest["results"]["unconditional_worst_window"]["return"],
        -0.1086821194,
        atol=1e-10,
    )
    assert manifest["results"]["conditional_windows"]["0.050000"][
        "lookback_sweep"
    ]["stable"]
    assert manifest["results"]["conditional_windows"]["0.100000"][
        "lookback_sweep"
    ]["stable"]
    assert np.isclose(
        manifest["results"]["stress_shape_bracket"]["four_session_loss"],
        0.19779042,
        atol=1e-8,
    )
    assert not manifest["results"]["stress_shape_bracket"]["is_estimate"]
    assert np.isclose(
        manifest["results"]["economic_compensation_loss_ceilings"][1][
            "maximum_supported_nav_loss"
        ],
        0.04448793,
        atol=1e-8,
    )


def _run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} RWA drawdown tests passed.")


if __name__ == "__main__":
    _run_all()
