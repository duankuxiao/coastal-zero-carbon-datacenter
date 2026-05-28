from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pandas as pd
import pytest

from scripts import run_country_growth_allocation as country_growth_allocation_module
from scripts.run_country_growth_allocation import (
    append_scale_totals,
    build_city_scale_allocations,
    build_cooling_comparison_results,
    build_country_average_results,
    build_country_growths,
    build_optimization_comparison_results,
    choose_facility_count,
    load_scale_definitions,
    run_cooling_comparisons,
    run_country_growth_cooling_comparison,
    run_country_growth_allocation,
    run_country_growth_load_shift_optimization,
    select_all_scale_results,
)


def test_growth_mw_is_2030_scenario_minus_2025_baseline():
    rows = [
        {
            "country": "A",
            "coastal_share_of_total_pct": 40.0,
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
    assert base["coastal_share_of_total_pct"] == pytest.approx(40.0)
    assert base["coastal_growth_mw"] == pytest.approx(200.0)


def test_each_city_gets_country_coastal_growth_instead_of_total_growth():
    allocations = _sample_allocations(country_growth_mw=120.0, coastal_share_pct=25.0)

    assert set(allocations["city"]) == {"A City", "B City"}
    assert allocations.groupby("city")["country_growth_mw"].first().to_dict() == {
        "A City": 120.0,
        "B City": 120.0,
    }
    assert allocations.groupby("city")["city_growth_mw"].first().to_dict() == {
        "A City": 30.0,
        "B City": 30.0,
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


def test_paper_city_results_keep_only_all_scale_totals():
    scale_results = pd.DataFrame(
        [
            _scale_result("small", "air_source", 10.0),
            _scale_result("medium", "air_source", 30.0),
            _scale_result("large", "air_source", 60.0),
        ]
    )

    with_totals = append_scale_totals(
        scale_results,
        metric_columns=["total_energy_kwh"],
        extra_group_columns=["cooling_type"],
    )
    paper_results = select_all_scale_results(with_totals)

    assert paper_results["scale"].tolist() == ["all_scales"]
    assert paper_results.iloc[0]["total_energy_kwh"] == pytest.approx(100.0)


def test_cooling_comparison_uses_air_source_as_baseline():
    results = pd.DataFrame(
        [
            _city_result("A City", 100.0, cooling_type="air_source"),
            _city_result("A City", 70.0, cooling_type="seawater"),
        ]
    )

    comparison = build_cooling_comparison_results(results)

    assert comparison.iloc[0]["air_source_total_energy_kwh"] == pytest.approx(100.0)
    assert comparison.iloc[0]["seawater_total_energy_kwh"] == pytest.approx(70.0)
    assert comparison.iloc[0]["total_energy_kwh_savings_vs_air_source"] == pytest.approx(30.0)
    assert comparison.iloc[0]["total_energy_kwh_savings_pct_vs_air_source"] == pytest.approx(30.0)


def test_optimization_comparison_uses_baseline_scenario_as_baseline():
    results = pd.DataFrame(
        [
            _optimization_result("baseline", 100.0),
            _optimization_result("load_shift", 75.0),
            _optimization_result("load_shift_battery", 60.0),
            _optimization_result("baseline_air_source", 130.0, cooling_type="air_source"),
        ]
    )

    comparison = build_optimization_comparison_results(results)

    scenarios = set(comparison["comparison_optimization_scenario"])
    assert scenarios == {"load_shift", "load_shift_battery"}
    load_shift = comparison[comparison["comparison_optimization_scenario"] == "load_shift"].iloc[0]
    assert load_shift["baseline_grid_purchase_mwh"] == pytest.approx(100.0)
    assert load_shift["comparison_grid_purchase_mwh"] == pytest.approx(75.0)
    assert load_shift["grid_purchase_mwh_savings_vs_baseline"] == pytest.approx(25.0)


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


def test_parallel_cooling_uses_thread_safe_cache_for_duplicate_tasks():
    allocation = _scale_result("small", "air_source", total_energy_kwh=10.0)
    allocation["facility_count"] = 1
    allocation["facility_capacity_mw"] = 5.0
    allocations = pd.DataFrame([allocation, dict(allocation)])
    energy_calls: list[tuple[object, ...]] = []
    wind_calls: list[str] = []
    counter_lock = threading.Lock()

    def fake_energy(**kwargs):
        key = (kwargs["city"], kwargs["cooling_type"], kwargs["rated_it_power_kw"])
        time.sleep(0.01)
        with counter_lock:
            energy_calls.append(key)
        return _fake_energy_result(kwargs["cooling_type"], kwargs["rated_it_power_kw"])

    def fake_wind(**kwargs):
        time.sleep(0.01)
        with counter_lock:
            wind_calls.append(kwargs["city"])
        return _fake_wind_result()

    results = run_cooling_comparisons(
        allocations=allocations,
        workload_file="workload.csv",
        idle_power_fraction=0.3,
        hours=24,
        start_time="2025-01-01 00:00",
        time_alignment="start_time",
        max_carbon_gap_hours=6,
        hub_height_m=150.0,
        wind_loss_fraction=0.15,
        wind_cut_in=3.0,
        wind_rated=12.0,
        wind_cut_out=25.0,
        energy_cache={},
        wind_resource_cache={},
        energy_cache_locks={},
        wind_resource_cache_locks={},
        cache_locks_guard=threading.Lock(),
        energy_calculator=fake_energy,
        wind_calculator=fake_wind,
        workers=4,
    )

    assert len(results) == 4
    assert len(energy_calls) == 2
    assert set(key[1] for key in energy_calls) == {"air_source", "seawater"}
    assert len(wind_calls) == 1


def test_cooling_main_function_writes_city_and_country_summaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(country_growth_allocation_module, "ROOT_DIR", tmp_path / "root")
    output_files = run_country_growth_cooling_comparison(
        output_dir=tmp_path / "results",
        country_rows=_country_rows(country_growth_mw=100.0),
        city_rows=_city_rows(),
        scale_rows=_scale_rows(),
        energy_calculator=lambda **kwargs: _fake_energy_result(
            kwargs["cooling_type"],
            kwargs["rated_it_power_kw"],
        ),
        wind_calculator=lambda **kwargs: _fake_wind_result(),
        hours=24,
        workers=2,
    )

    assert output_files["cooling_city_summary_csv"].exists()
    assert output_files["cooling_country_summary_csv"].exists()
    assert output_files["cooling_issues_csv"].exists()
    city_summary = pd.read_csv(output_files["cooling_city_summary_csv"])
    assert set(city_summary["scale"]) == {"all_scales"}
    assert "total_energy_kwh_savings_vs_air_source" in city_summary.columns


def test_cooling_main_function_writes_issue_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(country_growth_allocation_module, "ROOT_DIR", tmp_path / "root")

    def fake_energy(**kwargs):
        result = _fake_energy_result(kwargs["cooling_type"], kwargs["rated_it_power_kw"])
        if kwargs["cooling_type"] == "seawater":
            result.unmet_cooling_energy_kwh = 1.5
            result.constraint_violation_hours = 2.0
            result.outfall_temperature_violation_hours = 1.0
            result.seawater_temperature_violation_hours = 1.0
            result.model_warning_count = 1
            result.model_warning_messages = "WARNING, the outlet temperature is higher than 60C: 61.000"
        return result

    output_files = run_country_growth_cooling_comparison(
        output_dir=tmp_path / "results",
        country_rows=_country_rows(country_growth_mw=100.0),
        city_rows=_city_rows(),
        scale_rows=_scale_rows(),
        energy_calculator=fake_energy,
        wind_calculator=lambda **kwargs: _fake_wind_result(),
        hours=24,
        workers=2,
    )

    issues = pd.read_csv(output_files["cooling_issues_csv"])
    assert {
        "unmet_cooling_load",
        "outfall_temperature_violation",
        "seawater_temperature_violation",
        "outlet_temperature_warning",
    }.issubset(set(issues["issue_type"]))


def test_cooling_main_function_reuses_root_cache_for_completed_cities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(country_growth_allocation_module, "ROOT_DIR", tmp_path)
    output_dir = tmp_path / "results"
    calls = {"energy": 0, "wind": 0}

    def fake_energy(**kwargs):
        calls["energy"] += 1
        return _fake_energy_result(kwargs["cooling_type"], kwargs["rated_it_power_kw"])

    def fake_wind(**kwargs):
        calls["wind"] += 1
        return _fake_wind_result()

    first_output_files = run_country_growth_cooling_comparison(
        output_dir=output_dir,
        country_rows=_country_rows(country_growth_mw=100.0),
        city_rows=_city_rows(),
        scale_rows=_scale_rows(),
        energy_calculator=fake_energy,
        wind_calculator=fake_wind,
        hours=24,
        workers=2,
    )
    first_energy_calls = calls["energy"]
    first_wind_calls = calls["wind"]
    assert first_energy_calls > 0
    assert first_wind_calls > 0
    first_city_shape = pd.read_csv(first_output_files["cooling_city_summary_csv"]).shape

    calls["energy"] = 0
    calls["wind"] = 0
    second_output_files = run_country_growth_cooling_comparison(
        output_dir=output_dir,
        country_rows=_country_rows(country_growth_mw=100.0),
        city_rows=_city_rows(),
        scale_rows=_scale_rows(),
        energy_calculator=fake_energy,
        wind_calculator=fake_wind,
        hours=24,
        workers=2,
    )

    cache_dirs = list((tmp_path / "country_growth_cache").glob("country_growth_cooling_scale_cache_24h_*"))
    assert len(cache_dirs) == 1
    assert cache_dirs[0].is_dir()
    cache_files = list(cache_dirs[0].glob("*.csv"))
    assert len(cache_files) == 1
    assert not (output_dir / "country_growth_cache").exists()
    assert calls == {"energy": 0, "wind": 0}
    assert first_city_shape == pd.read_csv(second_output_files["cooling_city_summary_csv"]).shape


def test_cooling_uses_country_level_workers_and_country_cache_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(country_growth_allocation_module, "ROOT_DIR", tmp_path)
    country_rows = [_country_row("Country A", 100.0), _country_row("Country B", 100.0)]
    city_rows = [
        {"country": "Country A", "datacentermap_market": "A City", "toolkit_ready": True},
        {"country": "Country B", "datacentermap_market": "B City", "toolkit_ready": True},
    ]
    active_energy_calls = 0
    max_active_energy_calls = 0
    counter_lock = threading.Lock()

    def fake_energy(**kwargs):
        nonlocal active_energy_calls, max_active_energy_calls
        with counter_lock:
            active_energy_calls += 1
            max_active_energy_calls = max(max_active_energy_calls, active_energy_calls)
        time.sleep(0.01)
        with counter_lock:
            active_energy_calls -= 1
        return _fake_energy_result(kwargs["cooling_type"], kwargs["rated_it_power_kw"])

    run_country_growth_cooling_comparison(
        output_dir=tmp_path / "results",
        country_rows=country_rows,
        city_rows=city_rows,
        scale_rows=_scale_rows(),
        energy_calculator=fake_energy,
        wind_calculator=lambda **kwargs: _fake_wind_result(),
        hours=24,
        workers=2,
    )

    cache_dirs = list((tmp_path / "country_growth_cache").glob("country_growth_cooling_scale_cache_24h_*"))
    assert len(cache_dirs) == 1
    cache_files = list(cache_dirs[0].glob("*.csv"))
    assert len(cache_files) == 2
    assert {pd.read_csv(path)["country"].iloc[0] for path in cache_files} == {"Country A", "Country B"}
    assert max_active_energy_calls > 1


def test_load_shift_main_function_excludes_battery_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(country_growth_allocation_module, "ROOT_DIR", tmp_path / "root")
    output_files = run_country_growth_load_shift_optimization(
        output_dir=tmp_path / "results",
        country_rows=_country_rows(country_growth_mw=100.0),
        city_rows=_city_rows(),
        scale_rows=_scale_rows(),
        energy_calculator=lambda **kwargs: _fake_energy_result(
            kwargs["cooling_type"],
            kwargs["rated_it_power_kw"],
        ),
        wind_calculator=lambda **kwargs: _fake_wind_result(),
        optimizer=_fake_optimizer,
        hours=24,
        workers=2,
    )

    assert output_files["load_shift_city_summary_csv"].exists()
    assert output_files["load_shift_country_summary_csv"].exists()
    city_summary = pd.read_csv(output_files["load_shift_city_summary_csv"])
    assert set(city_summary["objective"]) == {"min-grid-co2"}
    assert set(city_summary["comparison_optimization_scenario"]) == {"load_shift"}
    assert "load_shift_battery" not in set(city_summary["comparison_optimization_scenario"])
    assert "grid_purchase_mwh_savings_vs_baseline" in city_summary.columns


def _sample_allocations(country_growth_mw: float, coastal_share_pct: float = 100.0) -> pd.DataFrame:
    growths = build_country_growths(
        _country_rows(country_growth_mw=country_growth_mw, coastal_share_pct=coastal_share_pct)
    )
    scales = load_scale_definitions(_scale_rows())
    return build_city_scale_allocations(
        country_growths=growths,
        city_rows=_city_rows(),
        scale_definitions=scales,
    )


def _country_rows(country_growth_mw: float, coastal_share_pct: float = 100.0) -> list[dict[str, object]]:
    return [_country_row("Country", country_growth_mw, coastal_share_pct=coastal_share_pct)]


def _country_row(
    country: str,
    country_growth_mw: float,
    *,
    coastal_share_pct: float = 100.0,
) -> dict[str, object]:
    baseline_mw = 1000.0
    scenario_mw = baseline_mw + country_growth_mw
    return {
        "country": country,
        "coastal_share_of_total_pct": coastal_share_pct,
        "total_mw_2025": baseline_mw,
        "total_mw_2030_Base": scenario_mw,
        "total_mw_2030_Lift-Off": scenario_mw,
        "total_mw_2030_High Efficiency": scenario_mw,
        "total_mw_2030_Headwinds": scenario_mw,
    }


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


def _city_result(city: str, total_energy_kwh: float, cooling_type: str = "seawater") -> dict[str, object]:
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
        "cooling_type": cooling_type,
        "status": "ok",
        "error_message": "",
        "total_energy_kwh": total_energy_kwh,
    }


def _scale_result(scale: str, cooling_type: str, total_energy_kwh: float) -> dict[str, object]:
    row = _city_result("A City", total_energy_kwh, cooling_type=cooling_type)
    row["scale"] = scale
    row["scale_share"] = {"small": 0.2, "medium": 0.3, "large": 0.5}[scale]
    row["scale_capacity_mw"] = row["scale_share"] * 100.0
    return row


def _optimization_result(
    scenario: str,
    grid_purchase_mwh: float,
    cooling_type: str = "seawater",
) -> dict[str, object]:
    row = _city_result("A City", total_energy_kwh=1000.0, cooling_type=cooling_type)
    row.update(
        {
            "objective": "min-grid-mwh",
            "optimization_scenario": scenario,
            "optimization_scenario_label": scenario,
            "grid_purchase_mwh": grid_purchase_mwh,
        }
    )
    return row


def _fake_energy_result(cooling_type: str, rated_it_power_kw: float) -> SimpleNamespace:
    return SimpleNamespace(
        cooling_type=cooling_type,
        hours=24,
        simulation_start_time="2025-01-01 00:00:00",
        simulation_end_time="2025-01-01 23:00:00",
        time_alignment="start_time",
        rated_it_power_kw=rated_it_power_kw,
        it_energy_kwh=10.0,
        it_carbon_emissions_kgco2=1.0,
        cooling_energy_kwh=2.0,
        cooling_carbon_emissions_kgco2=0.2,
        total_energy_kwh=12.0,
        carbon_emissions_kgco2=1.2,
    )


def _fake_wind_result() -> SimpleNamespace:
    return SimpleNamespace(
        wind_generation_per_mw_mwh=100.0,
        mean_net_capacity_factor=0.4,
        point_id="point",
        wind_nc_file="wind.nc",
        wind_start_time="2025-01-01 00:00:00",
        wind_end_time="2025-12-31 23:00:00",
    )


def _fake_optimizer(**kwargs) -> dict[str, object]:
    grid_purchase = 80.0 if float(kwargs["load_shift_fraction"]) > 0 else 100.0
    return {
        "annual_demand_mwh": 120.0,
        "annual_wind_mwh": 120.0,
        "grid_purchase_mwh": grid_purchase,
        "grid_purchase_co2_kg": grid_purchase * 10.0,
        "wind_curtailment_mwh": 5.0,
        "battery_charge_mwh": 0.0,
        "battery_discharge_mwh": 0.0,
        "battery_conversion_loss_mwh": 0.0,
        "shifted_down_mwh": 20.0 if float(kwargs["load_shift_fraction"]) > 0 else 0.0,
        "shifted_up_mwh": 20.0 if float(kwargs["load_shift_fraction"]) > 0 else 0.0,
        "hours_with_grid_purchase": 10.0,
        "hours_with_curtailment": 2.0,
        "max_hourly_grid_purchase_mw": 3.0,
        "max_hourly_wind_curtailment_mw": 1.0,
        "max_hourly_battery_charge_mw": 0.0,
        "max_hourly_battery_discharge_mw": 0.0,
    }
