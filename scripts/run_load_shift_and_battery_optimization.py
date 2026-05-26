"""Batch runner for toolkit-ready coastal zero-carbon dispatch optimization.

This script runs the single-city optimizer from ``optimization.optimize_zero_carbon``
for every toolkit-ready city in ``data/coastal_datacenter_city_manifest.xlsx``.

It intentionally does not save 8760-hour per-city dispatch tables. Outputs are:

1. one city/scenario/objective result table;
2. one aggregate scenario comparison table;
3. one country-level scenario comparison table.

Edit the arguments in ``main()`` before running if you need a different cooling
mode, objective list, battery setting, or output directory. The baseline set
also includes an air-source case with no load shifting or battery.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Iterable

import pandas as pd

from energy.calculate_datacenter_energy import WORKLOAD_FILE, load_city_manifest
from optimization.optimize_zero_carbon import optimization
from renewables.calculate_wind_capacity import calculate_required_wind_capacity


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT_DIR / "results"
RESULT_METRICS = [
    "required_wind_capacity_mw",
    "datacenter_total_energy_mwh",
    "annual_demand_mwh",
    "annual_wind_mwh",
    "wind_coverage_mwh",
    "grid_purchase_mwh",
    "grid_purchase_co2_kg",
    "average_grid_carbon_intensity_g_per_kwh",
    "renewable_physical_coverage_fraction",
    "wind_curtailment_mwh",
    "battery_configured_capacity_mwh",
    "battery_required_capacity_mwh",
    "battery_charge_mwh",
    "battery_discharge_mwh",
    "battery_conversion_loss_mwh",
    "shifted_down_mwh",
    "shifted_up_mwh",
    "load_movement_budget_used_fraction",
    "hours_with_grid_purchase",
    "hours_with_curtailment",
    "max_hourly_grid_purchase_mw",
    "max_hourly_wind_curtailment_mw",
    "max_hourly_battery_charge_mw",
    "max_hourly_battery_discharge_mw",
]
SCENARIO_ORDER = ("baseline_air_source", "baseline", "load_shift", "load_shift_battery")
SCENARIO_LABELS = {
    "baseline_air_source": "baseline (air source)",
    "baseline": "baseline",
    "load_shift": "load shift",
    "load_shift_battery": "load shift + battery",
}
HOURLY_RESULT_KEYS = {
    "optimized_demand_mwh",
    "grid_purchase_hourly_mwh",
    "wind_curtailment_hourly_mwh",
    "battery_soc_mwh",
    "battery_charge_hourly_mwh",
    "battery_discharge_hourly_mwh",
}


def run_optimizations(
    *,
    cooling: str = "seawater",
    objectives: Iterable[str] = ("min-grid-mwh", "min-grid-co2"),
    workload_file: str | Path = WORKLOAD_FILE,
    rated_it_power_kw: float = 20000.0,
    idle_power_fraction: float = 0.35,
    hours: int = 8760,
    start_time: str | None = "2025-01-01 00:00",
    time_alignment: str | None = None,
    max_carbon_gap_hours: int = 6,
    battery_capacity_mwh: float = 535.4,
    battery_roundtrip_efficiency: float = 0.97,
    grid_import_limit_mw: float | None = None,
    battery_charge_limit_mw: float | None = 25.0,
    battery_discharge_limit_mw: float | None = 25.0,
    load_shift_fraction: float = 0.3,
    hub_height_m: float = 150.0,
    wind_loss_fraction: float = 0.15,
    wind_cut_in: float = 3.0,
    wind_rated: float = 12.0,
    wind_cut_out: float = 25.0,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Path]]:
    """Run optimization for all toolkit-ready cities and save aggregate CSVs."""
    output_path = _resolve_output_dir(output_dir)
    objective_list = tuple(objectives)
    city_rows = load_city_manifest()

    rows: list[dict[str, object]] = []
    cities = city_rows["datacentermap_market"].dropna().astype(str).tolist()
    for city_index, (_, city_row) in enumerate(city_rows.iterrows(), start=1):
        city = str(city_row["datacentermap_market"])
        print(f"Processing {city_index}/{len(cities)}: {city}")

        scenario_configs = _scenario_configs(
            cooling=cooling,
            load_shift_fraction=load_shift_fraction,
            battery_capacity_mwh=battery_capacity_mwh,
            battery_charge_limit_mw=battery_charge_limit_mw,
            battery_discharge_limit_mw=battery_discharge_limit_mw,
        )
        wind_capacities: dict[str, object] = {}
        wind_capacity_errors: dict[str, str] = {}
        for scenario_config in scenario_configs:
            scenario_cooling = str(scenario_config["cooling_type"])
            if scenario_cooling in wind_capacities or scenario_cooling in wind_capacity_errors:
                continue
            try:
                wind_capacities[scenario_cooling] = calculate_required_wind_capacity(
                    city=city,
                    cooling_type=scenario_cooling,
                    workload_file=workload_file,
                    rated_it_power_kw=rated_it_power_kw,
                    idle_power_fraction=idle_power_fraction,
                    hours=hours,
                    start_time=start_time,
                    time_alignment=time_alignment,
                    max_carbon_gap_hours=max_carbon_gap_hours,
                    hub_height_m=hub_height_m,
                    loss_fraction=wind_loss_fraction,
                    cut_in=wind_cut_in,
                    rated=wind_rated,
                    cut_out=wind_cut_out,
                    progress=False,
                )
            except Exception as exc:
                wind_capacity_errors[scenario_cooling] = str(exc)

        for scenario_config in scenario_configs:
            scenario = str(scenario_config["scenario"])
            scenario_cooling = str(scenario_config["cooling_type"])
            if scenario_cooling in wind_capacity_errors:
                error = wind_capacity_errors[scenario_cooling]
                rows.append(
                    _failed_row(
                        city_row,
                        scenario_cooling,
                        "wind-capacity",
                        error,
                        None,
                        scenario_config,
                    )
                )
                print(f"  {scenario}/wind-capacity failed: {error}")
                continue

            wind_capacity = wind_capacities[scenario_cooling]
            for objective in objective_list:
                try:
                    result = optimization(
                        city=city,
                        cooling=scenario_cooling,
                        wind_capacity_mw=wind_capacity.required_wind_capacity_mw,
                        wind_nc_file=wind_capacity.wind_nc_file,
                        workload_file=workload_file,
                        rated_it_power_kw=rated_it_power_kw,
                        battery_capacity_mwh=scenario_config["battery_capacity_mwh"],
                        battery_roundtrip_efficiency=battery_roundtrip_efficiency,
                        grid_import_limit_mw=grid_import_limit_mw,
                        battery_charge_limit_mw=scenario_config["battery_charge_limit_mw"],
                        battery_discharge_limit_mw=scenario_config["battery_discharge_limit_mw"],
                        load_shift_fraction=scenario_config["load_shift_fraction"],
                        hours=hours,
                        start_time=start_time,
                        time_alignment=time_alignment,
                        max_carbon_gap_hours=max_carbon_gap_hours,
                        hub_height_m=hub_height_m,
                        wind_loss_fraction=wind_loss_fraction,
                        wind_cut_in=wind_cut_in,
                        wind_rated=wind_rated,
                        wind_cut_out=wind_cut_out,
                        objective=objective,
                        include_hourly=True,
                        output_results=False,
                    )
                    rows.append(_city_result_row(city_row, wind_capacity, result, objective, scenario_config))
                    print(
                        f"  {scenario}/{objective}: "
                        f"grid={result['grid_purchase_mwh']:.2f} MWh"
                    )
                except Exception as exc:
                    rows.append(
                        _failed_row(
                            city_row,
                            cooling,
                            objective,
                            str(exc),
                            wind_capacity,
                            scenario_config,
                        )
                    )
                    print(f"  {scenario}/{objective} failed: {exc}")

    city_results = pd.DataFrame(rows)
    summary = _build_summary_table(city_results, objective_list, cooling, hours)
    country_summary = _build_country_summary_table(city_results, objective_list, cooling, hours)

    suffix = f"{_filename_token(cooling)}_{_hours_token(hours)}"
    city_results_file = output_path / f"strict_coastal_optimization_city_results_{suffix}.csv"
    summary_file = output_path / f"strict_coastal_optimization_summary_{suffix}.csv"
    country_summary_file = output_path / f"strict_coastal_optimization_country_summary_{suffix}.csv"
    city_results.to_csv(city_results_file, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_file, index=False, encoding="utf-8-sig")
    country_summary.to_csv(country_summary_file, index=False, encoding="utf-8-sig")

    output_files = {
        "city_results_csv": city_results_file,
        "summary_csv": summary_file,
        "country_summary_csv": country_summary_file,
    }
    print(f"City/objective results CSV: {city_results_file}")
    print(f"Summary comparison CSV: {summary_file}")
    print(f"Country scenario comparison CSV: {country_summary_file}")
    return city_results, summary, output_files


def _city_result_row(
    city_row: pd.Series,
    wind_capacity,
    result: dict[str, object],
    objective: str,
    scenario_config: dict[str, object],
) -> dict[str, object]:
    row = _base_city_row(city_row)
    row.update(_scenario_metadata(scenario_config))
    row.update(
        {
            "status": "ok",
            "error": "",
            "objective": objective,
            "cooling_type": wind_capacity.cooling_type,
            "point_id": wind_capacity.point_id,
            "wind_nc_file": wind_capacity.wind_nc_file,
            "rated_it_power_kw": wind_capacity.rated_it_power_kw,
            "hours": wind_capacity.hours,
            "required_wind_capacity_mw": wind_capacity.required_wind_capacity_mw,
            "datacenter_total_energy_mwh": wind_capacity.datacenter_total_energy_mwh,
            "wind_generation_per_mw_mwh": wind_capacity.wind_generation_per_mw_mwh,
            "wind_mean_net_capacity_factor": wind_capacity.mean_net_capacity_factor,
            "wind_start_time": wind_capacity.wind_start_time,
            "wind_end_time": wind_capacity.wind_end_time,
        }
    )
    row.update(result)
    row["wind_coverage_mwh"] = _wind_coverage_mwh(row)
    row["battery_required_capacity_mwh"] = (
        _battery_required_capacity_mwh(result) if bool(row["battery_enabled"]) else 0.0
    )
    for key in HOURLY_RESULT_KEYS:
        row.pop(key, None)
    row.pop("csv_files", None)
    return row


def _failed_row(
    city_row: pd.Series,
    cooling: str,
    objective: str,
    error: str,
    wind_capacity=None,
    scenario_config: dict[str, object] | None = None,
) -> dict[str, object]:
    row = _base_city_row(city_row)
    row.update(_scenario_metadata(scenario_config))
    row.update(
        {
            "status": "failed",
            "error": error,
            "objective": objective,
            "cooling_type": cooling,
            "point_id": getattr(wind_capacity, "point_id", ""),
            "wind_nc_file": getattr(wind_capacity, "wind_nc_file", ""),
            "rated_it_power_kw": getattr(wind_capacity, "rated_it_power_kw", math.nan),
            "hours": getattr(wind_capacity, "hours", math.nan),
            "required_wind_capacity_mw": getattr(wind_capacity, "required_wind_capacity_mw", math.nan),
            "datacenter_total_energy_mwh": getattr(wind_capacity, "datacenter_total_energy_mwh", math.nan),
            "wind_generation_per_mw_mwh": getattr(wind_capacity, "wind_generation_per_mw_mwh", math.nan),
            "wind_mean_net_capacity_factor": getattr(wind_capacity, "mean_net_capacity_factor", math.nan),
            "wind_start_time": getattr(wind_capacity, "wind_start_time", None),
            "wind_end_time": getattr(wind_capacity, "wind_end_time", None),
        }
    )
    return row


def _scenario_configs(
    *,
    cooling: str,
    load_shift_fraction: float,
    battery_capacity_mwh: float,
    battery_charge_limit_mw: float | None,
    battery_discharge_limit_mw: float | None,
) -> tuple[dict[str, object], ...]:
    return (
        {
            "scenario": "baseline_air_source",
            "cooling_type": "air_source",
            "load_shift_enabled": False,
            "battery_enabled": False,
            "load_shift_fraction": 0.0,
            "battery_capacity_mwh": 0.0,
            "battery_charge_limit_mw": 0.0,
            "battery_discharge_limit_mw": 0.0,
        },
        {
            "scenario": "baseline",
            "cooling_type": cooling,
            "load_shift_enabled": False,
            "battery_enabled": False,
            "load_shift_fraction": 0.0,
            "battery_capacity_mwh": 0.0,
            "battery_charge_limit_mw": 0.0,
            "battery_discharge_limit_mw": 0.0,
        },
        {
            "scenario": "load_shift",
            "cooling_type": cooling,
            "load_shift_enabled": True,
            "battery_enabled": False,
            "load_shift_fraction": load_shift_fraction,
            "battery_capacity_mwh": 0.0,
            "battery_charge_limit_mw": 0.0,
            "battery_discharge_limit_mw": 0.0,
        },
        {
            "scenario": "load_shift_battery",
            "cooling_type": cooling,
            "load_shift_enabled": True,
            "battery_enabled": battery_capacity_mwh > 0.0,
            "load_shift_fraction": load_shift_fraction,
            "battery_capacity_mwh": battery_capacity_mwh,
            "battery_charge_limit_mw": battery_charge_limit_mw,
            "battery_discharge_limit_mw": battery_discharge_limit_mw,
        },
    )


def _scenario_metadata(scenario_config: dict[str, object] | None) -> dict[str, object]:
    if scenario_config is None:
        return {
            "scenario": "all",
            "scenario_label": "all",
            "load_shift_enabled": "",
            "battery_enabled": "",
            "configured_load_shift_fraction": math.nan,
            "battery_configured_capacity_mwh": math.nan,
            "battery_charge_limit_mw": math.nan,
            "battery_discharge_limit_mw": math.nan,
            "battery_required_capacity_mwh": math.nan,
        }

    scenario = str(scenario_config["scenario"])
    return {
        "scenario": scenario,
        "scenario_label": SCENARIO_LABELS.get(scenario, scenario),
        "load_shift_enabled": bool(scenario_config["load_shift_enabled"]),
        "battery_enabled": bool(scenario_config["battery_enabled"]),
        "configured_load_shift_fraction": float(scenario_config["load_shift_fraction"]),
        "battery_configured_capacity_mwh": float(scenario_config["battery_capacity_mwh"]),
        "battery_charge_limit_mw": scenario_config["battery_charge_limit_mw"],
        "battery_discharge_limit_mw": scenario_config["battery_discharge_limit_mw"],
        "battery_required_capacity_mwh": 0.0,
    }


def _base_city_row(city_row: pd.Series) -> dict[str, object]:
    return {
        "country_area": city_row.get("country", ""),
        "region": "",
        "city": city_row.get("datacentermap_market", ""),
        "city_metro_type": "datacentermap_market",
        "coastal_class": city_row.get("selection_status", ""),
    }


def _build_summary_table(
    city_results: pd.DataFrame,
    objectives: Iterable[str],
    cooling: str,
    hours: int,
) -> pd.DataFrame:
    ok = city_results[city_results["status"] == "ok"].copy() if "status" in city_results else pd.DataFrame()
    rows: list[dict[str, object]] = []

    for objective in objectives:
        objective_rows = [
            _aggregate_scenario(ok, objective, scenario, cooling, hours)
            for scenario in SCENARIO_ORDER
        ]
        baseline = next(row for row in objective_rows if row["scenario"] == "baseline")
        for row in objective_rows:
            _add_baseline_savings(row, baseline)
            rows.append(row)

    failed_count = int((city_results["status"] == "failed").sum()) if "status" in city_results else 0
    rows.append(
        {
            "scope": "failed_runs",
            "value_type": "count",
            "objective": "all",
            "scenario": "all",
            "scenario_label": "all",
            "cooling_type": cooling,
            "included_city_count": failed_count,
            "hours_per_city": hours,
        }
    )
    return pd.DataFrame(rows)


def _build_country_summary_table(
    city_results: pd.DataFrame,
    objectives: Iterable[str],
    cooling: str,
    hours: int,
) -> pd.DataFrame:
    ok = city_results[city_results["status"] == "ok"].copy() if "status" in city_results else pd.DataFrame()
    rows: list[dict[str, object]] = []
    country_column = "country_area"

    if ok.empty or country_column not in ok:
        return pd.DataFrame(rows)

    ok[country_column] = ok[country_column].fillna("").astype(str)
    countries = sorted(country for country in ok[country_column].unique() if country.strip())
    for objective in objectives:
        for country in countries:
            country_results = ok[ok[country_column] == country]
            objective_rows = [
                _aggregate_scenario(country_results, objective, scenario, cooling, hours)
                for scenario in SCENARIO_ORDER
            ]
            baseline = next(row for row in objective_rows if row["scenario"] == "baseline")
            for row in objective_rows:
                row["country_area"] = country
                row["scope"] = f"country:{country}"
                _add_baseline_savings(row, baseline)
                rows.append(row)

    columns = [
        "country_area",
        "scope",
        "value_type",
        "objective",
        "scenario",
        "scenario_label",
        "cooling_type",
        "included_city_count",
        "hours_per_city",
        *RESULT_METRICS,
        "energy_savings_mwh_vs_baseline",
        "energy_savings_pct_vs_baseline",
        "co2_savings_kg_vs_baseline",
        "co2_savings_pct_vs_baseline",
        "grid_purchase_savings_mwh_vs_baseline",
        "grid_purchase_savings_pct_vs_baseline",
    ]
    return pd.DataFrame(rows, columns=columns)


def _aggregate_scenario(
    ok_results: pd.DataFrame,
    objective: str,
    scenario: str,
    cooling: str,
    hours: int,
) -> dict[str, object]:
    if ok_results.empty:
        subset = ok_results
    else:
        subset = ok_results[
            (ok_results["objective"] == objective)
            & (ok_results["scenario"] == scenario)
        ]
    row: dict[str, object] = {
        "scope": scenario,
        "value_type": "absolute",
        "objective": objective,
        "scenario": scenario,
        "scenario_label": SCENARIO_LABELS.get(scenario, scenario),
        "cooling_type": _aggregate_cooling_type(subset, scenario, cooling),
        "included_city_count": int(subset["city"].nunique()) if not subset.empty else 0,
        "hours_per_city": hours,
    }
    for metric in RESULT_METRICS:
        row[metric] = _aggregate_metric(subset, metric)

    grid_mwh = float(row.get("grid_purchase_mwh", 0.0) or 0.0)
    grid_co2 = float(row.get("grid_purchase_co2_kg", 0.0) or 0.0)
    demand = float(row.get("annual_demand_mwh", 0.0) or 0.0)
    shifted_down = float(row.get("shifted_down_mwh", 0.0) or 0.0)
    row["wind_coverage_mwh"] = demand - grid_mwh
    row["average_grid_carbon_intensity_g_per_kwh"] = grid_co2 / grid_mwh if grid_mwh else 0.0
    row["renewable_physical_coverage_fraction"] = row["wind_coverage_mwh"] / demand if demand else math.nan
    row["load_movement_budget_used_fraction"] = shifted_down / demand if demand else math.nan
    return row


def _aggregate_cooling_type(results: pd.DataFrame, scenario: str, default_cooling: str) -> str:
    if not results.empty and "cooling_type" in results:
        cooling_types = sorted(
            str(value)
            for value in results["cooling_type"].dropna().unique()
            if str(value).strip()
        )
        if len(cooling_types) == 1:
            return cooling_types[0]
        if cooling_types:
            return "|".join(cooling_types)
    if scenario == "baseline_air_source":
        return "air_source"
    return default_cooling


def _aggregate_metric(results: pd.DataFrame, metric: str) -> float:
    if results.empty or metric not in results:
        return 0.0
    values = pd.to_numeric(results[metric], errors="coerce")
    if metric.startswith("max_hourly_"):
        return float(values.max()) if values.notna().any() else 0.0
    if metric in {
        "average_grid_carbon_intensity_g_per_kwh",
        "renewable_physical_coverage_fraction",
        "load_movement_budget_used_fraction",
    }:
        return float(values.mean()) if values.notna().any() else math.nan
    return float(values.sum())


def _add_baseline_savings(row: dict[str, object], baseline: dict[str, object]) -> None:
    energy_savings = _metric_savings(baseline, row, "datacenter_total_energy_mwh")
    co2_savings = _metric_savings(baseline, row, "grid_purchase_co2_kg")
    grid_savings = _metric_savings(baseline, row, "grid_purchase_mwh")
    row["energy_savings_mwh_vs_baseline"] = energy_savings
    row["energy_savings_pct_vs_baseline"] = _pct(energy_savings, baseline.get("datacenter_total_energy_mwh", 0.0))
    row["co2_savings_kg_vs_baseline"] = co2_savings
    row["co2_savings_pct_vs_baseline"] = _pct(co2_savings, baseline.get("grid_purchase_co2_kg", 0.0))
    row["grid_purchase_savings_mwh_vs_baseline"] = grid_savings
    row["grid_purchase_savings_pct_vs_baseline"] = _pct(grid_savings, baseline.get("grid_purchase_mwh", 0.0))


def _metric_savings(
    baseline: dict[str, object],
    other: dict[str, object],
    metric: str,
) -> float:
    return float(baseline.get(metric, 0.0) or 0.0) - float(other.get(metric, 0.0) or 0.0)


def _pct(numerator: float, denominator: object) -> float:
    denominator_float = float(denominator or 0.0)
    if math.isclose(denominator_float, 0.0):
        return math.nan
    return numerator / denominator_float * 100.0


def _wind_coverage_mwh(row: dict[str, object]) -> float:
    demand = float(row.get("annual_demand_mwh", row.get("datacenter_total_energy_mwh", 0.0)) or 0.0)
    grid_purchase = float(row.get("grid_purchase_mwh", 0.0) or 0.0)
    return demand - grid_purchase


def _battery_required_capacity_mwh(result: dict[str, object]) -> float:
    soc = result.get("battery_soc_mwh")
    if soc is None:
        return 0.0
    values = pd.to_numeric(pd.Series(soc), errors="coerce").dropna()
    if values.empty:
        return 0.0
    return float(values.max() - values.min())


def _resolve_output_dir(path: str | Path) -> Path:
    output_path = Path(path)
    if not output_path.is_absolute():
        output_path = ROOT_DIR / output_path
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path


def _filename_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", str(value).strip())
    return token.strip("_") or "unknown"


def _hours_token(hours: int | None) -> str:
    return "all_hours" if hours is None else f"{hours}h"


def main() -> None:
    _, _, output_files = run_optimizations(
        cooling="seawater",
        objectives=("min-grid-mwh", "min-grid-co2"),
        workload_file=WORKLOAD_FILE,
        rated_it_power_kw=20000.0,
        idle_power_fraction=0.35,
        hours=8760,
        start_time="2025-01-01 00:00",
        time_alignment=None,
        max_carbon_gap_hours=6,
        battery_capacity_mwh=535.4,
        battery_roundtrip_efficiency=0.97,
        grid_import_limit_mw=None,
        battery_charge_limit_mw=25.0,
        battery_discharge_limit_mw=25.0,
        load_shift_fraction=0.3,
        hub_height_m=150.0,
        wind_loss_fraction=0.15,
        wind_cut_in=3.0,
        wind_rated=12.0,
        wind_cut_out=25.0,
        output_dir=DEFAULT_OUTPUT_DIR,
    )
    print(json.dumps({key: str(path) for key, path in output_files.items()}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
