"""Batch runner for strict-coastal zero-carbon dispatch optimization.

This script runs the single-city optimizer from ``optimization.optimize_zero_carbon``
for every city marked ``Strict coastal`` in ``data/target_city_map.csv``.

It intentionally does not save 8760-hour per-city dispatch tables. Outputs are:

1. one city/objective result table;
2. one aggregate objective comparison table.

Edit the arguments in ``main()`` before running if you need a different cooling
mode, objective list, battery setting, or output directory.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Iterable

import pandas as pd

from energy.calculate_datacenter_energy import CITY_MAP_FILE, WORKLOAD_FILE
from optimization.optimize_zero_carbon import optimization
from renewables.calculate_wind_capacity import calculate_required_wind_capacity


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT_DIR / "results"
RESULT_METRICS = [
    "required_wind_capacity_mw",
    "datacenter_total_energy_mwh",
    "annual_demand_mwh",
    "annual_wind_mwh",
    "grid_purchase_mwh",
    "grid_purchase_co2_kg",
    "average_grid_carbon_intensity_g_per_kwh",
    "renewable_physical_coverage_fraction",
    "wind_curtailment_mwh",
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


def run_strict_coastal_optimizations(
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
    grid_import_limit_mw: float | None = 25.0,
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
    """Run optimization for all strict-coastal cities and save aggregate CSVs."""
    output_path = _resolve_output_dir(output_dir)
    objective_list = tuple(objectives)
    city_map = pd.read_csv(CITY_MAP_FILE)
    strict_coastal = city_map[
        city_map["Coastal class"].astype(str).str.strip().str.lower() == "strict coastal"
    ].copy()

    rows: list[dict[str, object]] = []
    cities = strict_coastal["City / metro"].dropna().astype(str).tolist()
    for city_index, (_, city_row) in enumerate(strict_coastal.iterrows(), start=1):
        city = str(city_row["City / metro"])
        print(f"Processing {city_index}/{len(cities)}: {city}")

        try:
            wind_capacity = calculate_required_wind_capacity(
                city=city,
                cooling_type=cooling,
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
            rows.append(_failed_row(city_row, cooling, "wind-capacity", str(exc)))
            print(f"  skipped: {exc}")
            continue

        for objective in objective_list:
            try:
                result = optimization(
                    city=city,
                    cooling=cooling,
                    wind_capacity_mw=wind_capacity.required_wind_capacity_mw,
                    wind_nc_file=wind_capacity.wind_nc_file,
                    workload_file=workload_file,
                    rated_it_power_kw=rated_it_power_kw,
                    battery_capacity_mwh=battery_capacity_mwh,
                    battery_roundtrip_efficiency=battery_roundtrip_efficiency,
                    grid_import_limit_mw=grid_import_limit_mw,
                    battery_charge_limit_mw=battery_charge_limit_mw,
                    battery_discharge_limit_mw=battery_discharge_limit_mw,
                    load_shift_fraction=load_shift_fraction,
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
                    include_hourly=False,
                    output_results=False,
                )
                rows.append(_city_result_row(city_row, wind_capacity, result, objective))
                print(f"  {objective}: grid={result['grid_purchase_mwh']:.2f} MWh")
            except Exception as exc:
                rows.append(_failed_row(city_row, cooling, objective, str(exc), wind_capacity))
                print(f"  {objective} failed: {exc}")

    city_results = pd.DataFrame(rows)
    summary = _build_summary_table(city_results, objective_list, cooling, hours)

    suffix = f"{_filename_token(cooling)}_{_hours_token(hours)}"
    city_results_file = output_path / f"strict_coastal_optimization_city_results_{suffix}.csv"
    summary_file = output_path / f"strict_coastal_optimization_summary_{suffix}.csv"
    city_results.to_csv(city_results_file, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_file, index=False, encoding="utf-8-sig")

    output_files = {
        "city_results_csv": city_results_file,
        "summary_csv": summary_file,
    }
    print(f"City/objective results CSV: {city_results_file}")
    print(f"Summary comparison CSV: {summary_file}")
    return city_results, summary, output_files


def _city_result_row(
    city_row: pd.Series,
    wind_capacity,
    result: dict[str, object],
    objective: str,
) -> dict[str, object]:
    row = _base_city_row(city_row)
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
    row.pop("csv_files", None)
    return row


def _failed_row(
    city_row: pd.Series,
    cooling: str,
    objective: str,
    error: str,
    wind_capacity=None,
) -> dict[str, object]:
    row = _base_city_row(city_row)
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


def _base_city_row(city_row: pd.Series) -> dict[str, object]:
    return {
        "country_area": city_row.get("Country/Area", ""),
        "region": city_row.get("Region", ""),
        "city": city_row.get("City / metro", ""),
        "city_metro_type": city_row.get("City/metro type", ""),
        "coastal_class": city_row.get("Coastal class", ""),
    }


def _build_summary_table(
    city_results: pd.DataFrame,
    objectives: Iterable[str],
    cooling: str,
    hours: int,
) -> pd.DataFrame:
    ok = city_results[city_results["status"] == "ok"].copy()
    rows = [_aggregate_objective(ok, objective, cooling, hours) for objective in objectives]

    by_objective = {str(row["objective"]): row for row in rows}
    if "min-grid-mwh" in by_objective and "min-grid-co2" in by_objective:
        rows.append(
            _comparison_row(
                by_objective["min-grid-mwh"],
                by_objective["min-grid-co2"],
                scope="min_grid_co2_minus_min_grid_mwh",
                value_type="difference",
            )
        )
        rows.append(
            _comparison_row(
                by_objective["min-grid-mwh"],
                by_objective["min-grid-co2"],
                scope="min_grid_co2_pct_change_vs_min_grid_mwh",
                value_type="percent",
            )
        )

    failed_count = int((city_results["status"] == "failed").sum()) if "status" in city_results else 0
    rows.append(
        {
            "scope": "failed_runs",
            "value_type": "count",
            "objective": "all",
            "cooling_type": cooling,
            "included_city_count": failed_count,
            "hours_per_city": hours,
        }
    )
    return pd.DataFrame(rows)


def _aggregate_objective(
    ok_results: pd.DataFrame,
    objective: str,
    cooling: str,
    hours: int,
) -> dict[str, object]:
    subset = ok_results[ok_results["objective"] == objective]
    row: dict[str, object] = {
        "scope": objective.replace("-", "_"),
        "value_type": "absolute",
        "objective": objective,
        "cooling_type": cooling,
        "included_city_count": int(subset["city"].nunique()) if not subset.empty else 0,
        "hours_per_city": hours,
    }
    for metric in RESULT_METRICS:
        row[metric] = _aggregate_metric(subset, metric)

    grid_mwh = float(row.get("grid_purchase_mwh", 0.0) or 0.0)
    grid_co2 = float(row.get("grid_purchase_co2_kg", 0.0) or 0.0)
    demand = float(row.get("annual_demand_mwh", 0.0) or 0.0)
    shifted_down = float(row.get("shifted_down_mwh", 0.0) or 0.0)
    row["average_grid_carbon_intensity_g_per_kwh"] = grid_co2 / grid_mwh if grid_mwh else 0.0
    row["renewable_physical_coverage_fraction"] = 1.0 - grid_mwh / demand if demand else math.nan
    row["load_movement_budget_used_fraction"] = shifted_down / demand if demand else math.nan
    return row


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


def _comparison_row(
    base: dict[str, object],
    other: dict[str, object],
    *,
    scope: str,
    value_type: str,
) -> dict[str, object]:
    row: dict[str, object] = {
        "scope": scope,
        "value_type": value_type,
        "objective": "min-grid-co2_vs_min-grid-mwh",
        "cooling_type": other.get("cooling_type", ""),
        "included_city_count": min(
            int(base.get("included_city_count", 0) or 0),
            int(other.get("included_city_count", 0) or 0),
        ),
        "hours_per_city": other.get("hours_per_city", ""),
    }
    for metric in RESULT_METRICS:
        base_value = float(base.get(metric, 0.0) or 0.0)
        other_value = float(other.get(metric, 0.0) or 0.0)
        diff = other_value - base_value
        if value_type == "percent":
            row[metric] = diff / base_value * 100.0 if not math.isclose(base_value, 0.0) else math.nan
        else:
            row[metric] = diff
    return row


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
    _, _, output_files = run_strict_coastal_optimizations(
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
        grid_import_limit_mw=25.0,
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
