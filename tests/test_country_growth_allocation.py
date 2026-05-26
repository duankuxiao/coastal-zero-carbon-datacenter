from pathlib import Path

import pandas as pd
import pytest

from scripts.run_country_growth_allocation import (
    build_city_scale_allocations,
    build_country_average_results,
    build_country_growths,
    choose_facility_count,
    load_scale_definitions,
    run_country_growth_allocation,
)


def test_growth_mw_is_2030_scenario_minus_2025_baseline():
    rows = [
        {
            "country": "A",
            "total_gw_2025": 1.5,
            "total_gw_2030_Base": 2.0,
            "total_gw_2030_Lift-Off": 2.5,
            "total_gw_2030_High Efficiency": 1.8,
            "total_gw_2030_Headwinds": 1.6,
        }
    ]

    growths = build_country_growths(rows)

    base = growths[growths["growth_scenario"] == "Base"].iloc[0]
    assert base["baseline_capacity_mw"] == pytest.approx(1500.0)
    assert base["scenario_capacity_mw"] == pytest.approx(2000.0)
    assert base["growth_mw"] == pytest.approx(500.0)


def test_each_city_gets_full_country_growth_instead_of_dividing_by_city_count():
    allocations = _sample_allocations(country_growth_mw=120.0)

    assert set(allocations["city"]) == {"A City", "B City"}
    assert allocations.groupby("city")["city_growth_mw"].first().to_dict() == {
        "A City": 120.0,
        "B City": 120.0,
    }


def test_country_results_average_city_results_instead_of_summing():
    city_results = pd.DataFrame(
        [
            _city_result("A City", 10.0),
            _city_result("B City", 30.0),
        ]
    )

    country = build_country_average_results(
        city_results,
        metric_columns=["total_energy_kwh"],
        extra_group_columns=["cooling_type", "scale"],
    )

    assert country.iloc[0]["representative_city_count"] == 2
    assert country.iloc[0]["total_energy_kwh"] == pytest.approx(20.0)


def test_scale_allocation_preserves_city_capacity():
    allocations = _sample_allocations(country_growth_mw=100.0)

    totals = allocations.groupby(["country", "growth_scenario", "city"])["scale_capacity_mw"].sum()

    allocated = allocations.groupby(["country", "growth_scenario", "city"])["allocated_capacity_mw"].sum()
    assert all(value == pytest.approx(100.0) for value in totals)
    assert all(value == pytest.approx(100.0) for value in allocated)


def test_choose_facility_count_targets_capacity_range_midpoint():
    split = choose_facility_count(total_mw=50.0, min_mw=10.0, max_mw=20.0)

    assert split.facility_count == 3
    assert split.facility_capacity_mw == pytest.approx(50.0 / 3.0)
    assert split.below_scale_min is False


def test_choose_facility_count_does_not_inflate_capacity_below_minimum():
    split = choose_facility_count(total_mw=5.0, min_mw=10.0, max_mw=20.0)

    assert split.facility_count == 1
    assert split.facility_capacity_mw == pytest.approx(5.0)
    assert split.below_scale_min is True


def test_dry_run_writes_growth_and_allocation_csvs_without_energy_model(tmp_path: Path):
    def fail_energy(**kwargs):
        raise AssertionError("dry-run must not call energy model")

    def fail_wind(**kwargs):
        raise AssertionError("dry-run must not call wind model")

    def fail_optimizer(**kwargs):
        raise AssertionError("dry-run must not call optimizer")

    output_files = run_country_growth_allocation(
        output_dir=tmp_path,
        dry_run=True,
        country_rows=_country_rows(country_growth_mw=100.0),
        city_rows=_city_rows(),
        scale_rows=_scale_rows(),
        energy_calculator=fail_energy,
        wind_calculator=fail_wind,
        optimizer=fail_optimizer,
    )

    assert output_files["country_growths_csv"].exists()
    assert output_files["city_scale_allocations_csv"].exists()
    assert pd.read_csv(output_files["country_growths_csv"]).shape[0] == 4
    assert pd.read_csv(output_files["city_scale_allocations_csv"]).shape[0] == 24


def _sample_allocations(country_growth_mw: float) -> pd.DataFrame:
    growths = build_country_growths(_country_rows(country_growth_mw=country_growth_mw))
    scales = load_scale_definitions(_scale_rows())
    return build_city_scale_allocations(
        country_growths=growths,
        city_rows=_city_rows(),
        scale_definitions=scales,
    )


def _country_rows(country_growth_mw: float) -> list[dict[str, object]]:
    baseline_mw = 1000.0
    scenario_mw = baseline_mw + country_growth_mw
    return [
        {
            "country": "Country",
            "total_mw_2025": baseline_mw,
            "total_mw_2030_Base": scenario_mw,
            "total_mw_2030_Lift-Off": scenario_mw,
            "total_mw_2030_High Efficiency": scenario_mw,
            "total_mw_2030_Headwinds": scenario_mw,
        }
    ]


def _city_rows() -> list[dict[str, object]]:
    return [
        {"country": "Country", "datacentermap_market": "A City", "toolkit_ready": True},
        {"country": "Country", "datacentermap_market": "B City", "toolkit_ready": "yes"},
        {"country": "Country", "datacentermap_market": "Not Ready", "toolkit_ready": False},
    ]


def _scale_rows() -> list[dict[str, object]]:
    return [
        {"category": "small", "ratio": 0.2, "lower_bound_mw": 1.0, "upper_bound_mw": 10.0},
        {"category": "medium", "ratio": 0.3, "lower_bound_mw": 10.0, "upper_bound_mw": 50.0},
        {"category": "large", "ratio": 0.5, "lower_bound_mw": 50.0, "upper_bound_mw": 200.0},
        {"category": None, "ratio": None, "lower_bound_mw": None, "upper_bound_mw": None},
    ]


def _city_result(city: str, total_energy_kwh: float) -> dict[str, object]:
    return {
        "country": "Country",
        "growth_scenario": "Base",
        "city": city,
        "scale": "all_scales",
        "city_count_in_country": 2,
        "country_growth_mw": 100.0,
        "city_growth_mw": 100.0,
        "scale_share": 1.0,
        "scale_capacity_mw": 100.0,
        "facility_count": 1,
        "facility_capacity_mw": 100.0,
        "below_scale_min": False,
        "cooling_type": "seawater",
        "status": "ok",
        "error_message": "",
        "total_energy_kwh": total_energy_kwh,
    }
