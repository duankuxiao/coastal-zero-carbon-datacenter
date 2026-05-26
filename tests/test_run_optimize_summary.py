import pandas as pd
import pytest

from scripts.run_load_shift_and_battery_optimization import (
    _build_country_summary_table,
    _build_summary_table,
)


def test_summary_compares_optimization_scenarios_against_baseline():
    city_results = pd.DataFrame(
        [
            _city_row("A", "baseline", 100.0, 40.0, 20000.0, 0.0),
            _city_row("B", "baseline", 50.0, 10.0, 4000.0, 0.0),
            _city_row("A", "load_shift", 100.0, 30.0, 12000.0, 0.0),
            _city_row("B", "load_shift", 50.0, 5.0, 1000.0, 0.0),
            _city_row("A", "load_shift_battery", 100.0, 20.0, 8000.0, 12.0),
            _city_row("B", "load_shift_battery", 50.0, 2.0, 500.0, 8.0),
        ]
    )

    summary = _build_summary_table(
        city_results,
        objectives=("min-grid-mwh",),
        cooling="seawater",
        hours=8760,
    )

    baseline = _summary_row(summary, "baseline")
    load_shift = _summary_row(summary, "load_shift")
    load_shift_battery = _summary_row(summary, "load_shift_battery")

    assert baseline["datacenter_total_energy_mwh"] == pytest.approx(150.0)
    assert baseline["wind_coverage_mwh"] == pytest.approx(100.0)
    assert baseline["renewable_physical_coverage_fraction"] == pytest.approx(100.0 / 150.0)

    assert load_shift["energy_savings_mwh_vs_baseline"] == pytest.approx(0.0)
    assert load_shift["co2_savings_kg_vs_baseline"] == pytest.approx(11000.0)
    assert load_shift["co2_savings_pct_vs_baseline"] == pytest.approx(11000.0 / 24000.0 * 100.0)
    assert load_shift["grid_purchase_savings_mwh_vs_baseline"] == pytest.approx(15.0)
    assert load_shift["grid_purchase_savings_pct_vs_baseline"] == pytest.approx(30.0)

    assert load_shift_battery["battery_required_capacity_mwh"] == pytest.approx(20.0)
    assert load_shift_battery["grid_purchase_savings_mwh_vs_baseline"] == pytest.approx(28.0)


def test_country_summary_compares_scenarios_within_each_country():
    city_results = pd.DataFrame(
        [
            _city_row("Shanghai", "baseline", 100.0, 40.0, 20000.0, 0.0, country="China"),
            _city_row("Tokyo", "baseline", 80.0, 20.0, 10000.0, 0.0, country="Japan"),
            _city_row("Shanghai", "load_shift", 100.0, 25.0, 10000.0, 0.0, country="China"),
            _city_row("Tokyo", "load_shift", 80.0, 10.0, 6000.0, 0.0, country="Japan"),
        ]
    )

    country_summary = _build_country_summary_table(
        city_results,
        objectives=("min-grid-mwh",),
        cooling="seawater",
        hours=8760,
    )

    china = _country_summary_row(country_summary, "China", "load_shift")
    japan = _country_summary_row(country_summary, "Japan", "load_shift")

    assert china["included_city_count"] == 1
    assert china["grid_purchase_savings_mwh_vs_baseline"] == pytest.approx(15.0)
    assert china["grid_purchase_savings_pct_vs_baseline"] == pytest.approx(37.5)
    assert china["co2_savings_kg_vs_baseline"] == pytest.approx(10000.0)

    assert japan["included_city_count"] == 1
    assert japan["grid_purchase_savings_mwh_vs_baseline"] == pytest.approx(10.0)
    assert japan["co2_savings_pct_vs_baseline"] == pytest.approx(40.0)


def _city_row(
    city: str,
    scenario: str,
    demand_mwh: float,
    grid_purchase_mwh: float,
    grid_purchase_co2_kg: float,
    battery_required_capacity_mwh: float,
    country: str = "Country",
) -> dict[str, object]:
    return {
        "status": "ok",
        "country_area": country,
        "city": city,
        "objective": "min-grid-mwh",
        "scenario": scenario,
        "datacenter_total_energy_mwh": demand_mwh,
        "annual_demand_mwh": demand_mwh,
        "annual_wind_mwh": demand_mwh,
        "grid_purchase_mwh": grid_purchase_mwh,
        "grid_purchase_co2_kg": grid_purchase_co2_kg,
        "wind_curtailment_mwh": grid_purchase_mwh,
        "battery_required_capacity_mwh": battery_required_capacity_mwh,
    }


def _summary_row(summary: pd.DataFrame, scenario: str) -> pd.Series:
    return summary[
        (summary["objective"] == "min-grid-mwh")
        & (summary["scenario"] == scenario)
    ].iloc[0]


def _country_summary_row(summary: pd.DataFrame, country: str, scenario: str) -> pd.Series:
    return summary[
        (summary["country_area"] == country)
        & (summary["objective"] == "min-grid-mwh")
        & (summary["scenario"] == scenario)
    ].iloc[0]
