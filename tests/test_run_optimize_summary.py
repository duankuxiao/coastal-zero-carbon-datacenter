import pandas as pd
import pytest

from utils.output_tables import write_optimization_output_tables


def test_optimization_output_tables_split_baseline_and_load_shift(tmp_path):
    city_results = pd.DataFrame(
        [
            _city_row("A", "baseline", 100.0, 40.0, 20000.0),
            _city_row("B", "baseline", 50.0, 10.0, 4000.0),
            _city_row("A", "load_shift", 100.0, 30.0, 12000.0),
            _city_row("B", "load_shift", 50.0, 5.0, 1000.0),
        ]
    )

    output_files = write_optimization_output_tables(
        city_results,
        tmp_path,
        hours=8760,
        country_metric_aggregation="sum",
        default_growth_scenario="Base",
    )

    assert output_files["city_seawater_baseline_csv"].exists()
    assert output_files["country_seawater_baseline_csv"].exists()
    assert output_files["city_seawater_load_shift_mwh_csv"].exists()
    assert output_files["country_seawater_load_shift_mwh_csv"].exists()

    baseline = pd.read_csv(output_files["city_seawater_baseline_csv"])
    load_shift = pd.read_csv(output_files["city_seawater_load_shift_mwh_csv"])
    assert baseline["grid_purchase_mwh"].sum() == pytest.approx(50.0)
    assert load_shift["grid_purchase_mwh"].sum() == pytest.approx(35.0)
    assert set(load_shift["growth_scenario"]) == {"Base"}

    country = pd.read_csv(output_files["country_seawater_load_shift_mwh_csv"])
    assert country.iloc[0]["total_energy_kwh"] == pytest.approx(150000.0)
    assert country.iloc[0]["grid_purchase_mwh"] == pytest.approx(35.0)
    assert country.iloc[0]["grid_purchase_co2_kg"] == pytest.approx(13000.0)
    assert country.iloc[0]["renewable_physical_coverage_fraction"] == pytest.approx(1.0 - 35.0 / 150.0)


def test_country_output_can_average_city_metrics(tmp_path):
    city_results = pd.DataFrame(
        [
            _city_row("Shanghai", "load_shift", 100.0, 25.0, 10000.0, country="China"),
            _city_row("Nantong", "load_shift", 80.0, 15.0, 6000.0, country="China"),
            _city_row("Tokyo", "load_shift", 60.0, 10.0, 5000.0, country="Japan"),
        ]
    )

    output_files = write_optimization_output_tables(
        city_results,
        tmp_path,
        hours=8760,
        country_metric_aggregation="mean",
        default_growth_scenario="Base",
    )

    country = pd.read_csv(output_files["country_seawater_load_shift_mwh_csv"])
    china = country[country["country"] == "China"].iloc[0]
    japan = country[country["country"] == "Japan"].iloc[0]

    assert china["grid_purchase_mwh"] == pytest.approx(20.0)
    assert china["grid_purchase_co2_kg"] == pytest.approx(8000.0)
    assert japan["grid_purchase_mwh"] == pytest.approx(10.0)


def _city_row(
    city: str,
    scenario: str,
    demand_mwh: float,
    grid_purchase_mwh: float,
    grid_purchase_co2_kg: float,
    country: str = "Country",
) -> dict[str, object]:
    return {
        "status": "ok",
        "country": country,
        "city": city,
        "growth_scenario": "Base",
        "cooling_type": "seawater",
        "objective": "min-grid-mwh",
        "optimization_scenario": scenario,
        "city_growth_mw": demand_mwh / 10.0,
        "datacenter_total_energy_mwh": demand_mwh,
        "annual_demand_mwh": demand_mwh,
        "annual_wind_mwh": demand_mwh,
        "grid_purchase_mwh": grid_purchase_mwh,
        "grid_purchase_co2_kg": grid_purchase_co2_kg,
        "wind_curtailment_mwh": grid_purchase_mwh,
    }
