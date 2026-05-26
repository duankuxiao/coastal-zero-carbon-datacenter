"""Country growth allocation runner for cooling and optimization comparisons.

The runner reads the country, city, and data-center scale sheets from
data/coastal_datacenter_city_manifest.xlsx. For each country and 2030 scenario,
every toolkit-ready representative city carries the country's full growth
capacity. The city capacity is split across small, medium, and large data-center
scales before running cooling and dispatch comparisons.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from energy.calculate_datacenter_energy import (
    CITY_MAP_FILE,
    DEFAULT_OUTPUT_DIR,
    WORKLOAD_FILE,
    DataCenterEnergyResult,
    _read_xlsx_sheet_rows,
    calculate_data_center_energy,
)
from optimization.optimize_zero_carbon import optimization
from renewables.calculate_wind_capacity import WindResourceResult, calculate_wind_resource
from scripts.run_load_shift_and_battery_optimization import (
    HOURLY_RESULT_KEYS as OPTIMIZATION_HOURLY_RESULT_KEYS,
    RESULT_METRICS as OPTIMIZATION_RESULT_METRICS,
    SCENARIO_LABELS as OPTIMIZATION_SCENARIO_LABELS,
    _scenario_configs,
)


ROOT_DIR = Path(__file__).resolve().parent.parent
COUNTRY_MANIFEST_SHEET = "Country_manifest"
CITY_MANIFEST_SHEET = "City_manifest"
DATACENTER_SCALE_SHEET = "Datacenter_scale"
COOLING_TYPES = ("air_source", "seawater")
COOLING_METRICS = [
    "server_energy_kwh",
    "server_carbon_emissions_kgco2",
    "cooling_energy_kwh",
    "cooling_carbon_emissions_kgco2",
    "total_energy_kwh",
    "total_carbon_emissions_kgco2",
    "required_wind_capacity_mw",
    "wind_annual_generation_mwh",
]
BASE_METADATA_COLUMNS = [
    "country",
    "growth_scenario",
    "city",
    "scale",
    "city_count_in_country",
    "country_growth_mw",
    "city_growth_mw",
    "scale_share",
    "scale_capacity_mw",
    "facility_count",
    "facility_capacity_mw",
    "below_scale_min",
]


@dataclass(frozen=True)
class ScaleDefinition:
    scale: str
    ratio: float
    min_capacity_mw: float
    max_capacity_mw: float


@dataclass(frozen=True)
class FacilitySplit:
    facility_count: int
    facility_capacity_mw: float
    below_scale_min: bool


@dataclass(frozen=True)
class RequiredWindCapacity:
    city: str
    cooling_type: str
    rated_it_power_kw: float
    hours: int
    datacenter_total_energy_mwh: float
    required_wind_capacity_mw: float
    wind_generation_per_mw_mwh: float
    mean_net_capacity_factor: float
    point_id: object
    wind_nc_file: object
    wind_start_time: object
    wind_end_time: object


EnergyCalculator = Callable[..., DataCenterEnergyResult]
WindCalculator = Callable[..., WindResourceResult]
Optimizer = Callable[..., dict[str, object]]


def run_country_growth_allocation(
    *,
    manifest_file: str | Path = CITY_MAP_FILE,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    include_not_ready: bool = False,
    dry_run: bool = False,
    workload_file: str | Path = WORKLOAD_FILE,
    idle_power_fraction: float = 0.35,
    hours: int | None = 8760,
    start_time: str | None = "2025-01-01 00:00",
    time_alignment: str | None = None,
    max_carbon_gap_hours: int = 6,
    cooling: str = "seawater",
    objectives: Iterable[str] = ("min-grid-mwh", "min-grid-co2"),
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
    country_rows: list[dict[str, object]] | None = None,
    city_rows: list[dict[str, object]] | None = None,
    scale_rows: list[dict[str, object]] | None = None,
    energy_calculator: EnergyCalculator = calculate_data_center_energy,
    wind_calculator: WindCalculator = calculate_wind_resource,
    optimizer: Optimizer = optimization,
) -> dict[str, Path]:
    """Run country-growth allocation and write CSV outputs."""
    manifest_path = _resolve_path(manifest_file)
    output_path = _resolve_output_dir(output_dir)
    if country_rows is None:
        country_rows = _read_xlsx_sheet_rows(manifest_path, COUNTRY_MANIFEST_SHEET)
    if city_rows is None:
        city_rows = _read_xlsx_sheet_rows(manifest_path, CITY_MANIFEST_SHEET)
    if scale_rows is None:
        scale_rows = _read_xlsx_sheet_rows(manifest_path, DATACENTER_SCALE_SHEET)

    country_growths = build_country_growths(country_rows)
    scale_definitions = load_scale_definitions(scale_rows)
    city_scale_allocations = build_city_scale_allocations(
        country_growths=country_growths,
        city_rows=city_rows,
        scale_definitions=scale_definitions,
        include_not_ready=include_not_ready,
    )

    output_files = _write_foundation_outputs(output_path, country_growths, city_scale_allocations)
    if dry_run:
        print(f"Dry run complete. Foundation CSVs written under {output_path}")
        return output_files

    energy_cache: dict[tuple[object, ...], DataCenterEnergyResult] = {}
    wind_resource_cache: dict[tuple[object, ...], WindResourceResult] = {}
    required_wind_cache: dict[tuple[object, ...], RequiredWindCapacity] = {}

    cooling_city_scale = run_cooling_comparisons(
        allocations=city_scale_allocations,
        workload_file=workload_file,
        idle_power_fraction=idle_power_fraction,
        hours=hours,
        start_time=start_time,
        time_alignment=_baseline_time_alignment(start_time, time_alignment),
        max_carbon_gap_hours=max_carbon_gap_hours,
        hub_height_m=hub_height_m,
        wind_loss_fraction=wind_loss_fraction,
        wind_cut_in=wind_cut_in,
        wind_rated=wind_rated,
        wind_cut_out=wind_cut_out,
        energy_cache=energy_cache,
        wind_resource_cache=wind_resource_cache,
        energy_calculator=energy_calculator,
        wind_calculator=wind_calculator,
    )
    cooling_city_results = append_scale_totals(cooling_city_scale, COOLING_METRICS, extra_group_columns=["cooling_type"])
    cooling_country_results = build_country_average_results(
        cooling_city_results,
        metric_columns=COOLING_METRICS,
        extra_group_columns=["cooling_type", "scale"],
    )

    optimization_city_scale = run_optimization_comparisons(
        allocations=city_scale_allocations,
        cooling=cooling,
        objectives=tuple(objectives),
        workload_file=workload_file,
        idle_power_fraction=idle_power_fraction,
        hours=hours,
        start_time=start_time,
        time_alignment=time_alignment,
        max_carbon_gap_hours=max_carbon_gap_hours,
        battery_capacity_mwh=battery_capacity_mwh,
        battery_roundtrip_efficiency=battery_roundtrip_efficiency,
        grid_import_limit_mw=grid_import_limit_mw,
        battery_charge_limit_mw=battery_charge_limit_mw,
        battery_discharge_limit_mw=battery_discharge_limit_mw,
        load_shift_fraction=load_shift_fraction,
        hub_height_m=hub_height_m,
        wind_loss_fraction=wind_loss_fraction,
        wind_cut_in=wind_cut_in,
        wind_rated=wind_rated,
        wind_cut_out=wind_cut_out,
        energy_cache=energy_cache,
        wind_resource_cache=wind_resource_cache,
        required_wind_cache=required_wind_cache,
        energy_calculator=energy_calculator,
        wind_calculator=wind_calculator,
        optimizer=optimizer,
    )
    optimization_city_results = append_scale_totals(
        optimization_city_scale,
        OPTIMIZATION_RESULT_METRICS,
        extra_group_columns=["objective", "optimization_scenario", "optimization_scenario_label", "cooling_type"],
    )
    optimization_country_results = build_country_average_results(
        optimization_city_results,
        metric_columns=OPTIMIZATION_RESULT_METRICS,
        extra_group_columns=[
            "objective",
            "optimization_scenario",
            "optimization_scenario_label",
            "cooling_type",
            "scale",
        ],
    )

    suffix = _hours_token(hours)
    files = {
        "cooling_city_results_csv": output_path / f"country_growth_cooling_city_results_{suffix}.csv",
        "optimization_city_results_csv": output_path / f"country_growth_optimization_city_results_{suffix}.csv",
        "cooling_country_results_csv": output_path / f"country_growth_cooling_country_results_{suffix}.csv",
        "optimization_country_results_csv": output_path / f"country_growth_optimization_country_results_{suffix}.csv",
    }
    cooling_city_results.to_csv(files["cooling_city_results_csv"], index=False, encoding="utf-8-sig")
    optimization_city_results.to_csv(files["optimization_city_results_csv"], index=False, encoding="utf-8-sig")
    cooling_country_results.to_csv(files["cooling_country_results_csv"], index=False, encoding="utf-8-sig")
    optimization_country_results.to_csv(files["optimization_country_results_csv"], index=False, encoding="utf-8-sig")
    output_files.update(files)
    for label, path in output_files.items():
        print(f"{label}: {path}")
    return output_files


def build_country_growths(country_rows: list[dict[str, object]]) -> pd.DataFrame:
    """Return country/scenario growth rows in MW."""
    if not country_rows:
        raise ValueError(f"Workbook sheet {COUNTRY_MANIFEST_SHEET} is empty.")
    columns = list(country_rows[0].keys())
    country_column = _find_column(columns, ["country", "country_area", "nation"], "country")
    baseline_column = _find_2025_capacity_column(columns)
    scenario_columns = _find_2030_capacity_columns(columns)

    rows: list[dict[str, object]] = []
    for source_row in country_rows:
        country = _text(source_row.get(country_column))
        if not country:
            continue
        baseline_mw = _capacity_to_mw(source_row.get(baseline_column), baseline_column)
        for scenario_column in scenario_columns:
            scenario_capacity_mw = _capacity_to_mw(source_row.get(scenario_column), scenario_column)
            growth_mw = scenario_capacity_mw - baseline_mw
            if growth_mw < 0:
                raise ValueError(
                    f"Negative growth for country={country}, scenario column={scenario_column}: "
                    f"{growth_mw:.6g} MW."
                )
            rows.append(
                {
                    "country": country,
                    "growth_scenario": _scenario_label_from_column(scenario_column),
                    "baseline_capacity_mw": baseline_mw,
                    "scenario_capacity_mw": scenario_capacity_mw,
                    "growth_mw": growth_mw,
                    "baseline_column": baseline_column,
                    "scenario_column": scenario_column,
                }
            )
    if not rows:
        raise ValueError(f"No country growth rows were built from sheet {COUNTRY_MANIFEST_SHEET}.")
    return pd.DataFrame(rows)


def load_scale_definitions(scale_rows: list[dict[str, object]]) -> list[ScaleDefinition]:
    """Return normalized small/medium/large scale definitions."""
    if not scale_rows:
        raise ValueError(f"Workbook sheet {DATACENTER_SCALE_SHEET} is empty.")
    columns = list(scale_rows[0].keys())
    scale_column = _find_column(columns, ["scale", "category", "size"], "scale")
    ratio_column = _find_column(columns, ["ratio", "share", "capacity_ratio"], "ratio")
    min_column = _find_column(
        columns,
        ["min_capacity_mw", "lower_bound_mw", "min_mw", "lower_mw"],
        "min capacity MW",
    )
    max_column = _find_column(
        columns,
        ["max_capacity_mw", "upper_bound_mw", "max_mw", "upper_mw"],
        "max capacity MW",
    )

    definitions: list[ScaleDefinition] = []
    for source_row in scale_rows:
        if not any(_text(value) for value in source_row.values()):
            continue
        if not _text(source_row.get(scale_column)):
            continue
        scale = _normalize_scale(source_row.get(scale_column))
        ratio = _number(source_row.get(ratio_column), f"{scale} ratio")
        min_mw = _number(source_row.get(min_column), f"{scale} min capacity")
        max_mw = _number(source_row.get(max_column), f"{scale} max capacity")
        if ratio < 0:
            raise ValueError(f"Scale ratio must be non-negative for {scale}.")
        if min_mw <= 0 or max_mw <= 0 or min_mw > max_mw:
            raise ValueError(f"Invalid capacity range for {scale}: {min_mw} to {max_mw} MW.")
        definitions.append(ScaleDefinition(scale, ratio, min_mw, max_mw))

    by_scale = {definition.scale: definition for definition in definitions}
    missing = [scale for scale in ("small", "medium", "large") if scale not in by_scale]
    if missing:
        raise ValueError(f"Missing data-center scale rows: {', '.join(missing)}")
    ordered = [by_scale[scale] for scale in ("small", "medium", "large")]
    ratio_sum = sum(definition.ratio for definition in ordered)
    if math.isclose(ratio_sum, 100.0, rel_tol=0.0, abs_tol=1e-6):
        ordered = [
            ScaleDefinition(
                definition.scale,
                definition.ratio / 100.0,
                definition.min_capacity_mw,
                definition.max_capacity_mw,
            )
            for definition in ordered
        ]
        ratio_sum = sum(definition.ratio for definition in ordered)
    if not math.isclose(ratio_sum, 1.0, rel_tol=0.0, abs_tol=1e-4):
        raise ValueError(f"Datacenter_scale ratios must sum to 1.0 or 100.0; got {ratio_sum:.8g}.")
    return ordered


def build_city_scale_allocations(
    *,
    country_growths: pd.DataFrame,
    city_rows: list[dict[str, object]],
    scale_definitions: list[ScaleDefinition],
    include_not_ready: bool = False,
) -> pd.DataFrame:
    """Assign each country's full growth to every representative city and scale."""
    cities_by_country = _cities_by_country(city_rows, include_not_ready=include_not_ready)
    rows: list[dict[str, object]] = []
    for growth_row in country_growths.to_dict(orient="records"):
        country = _text(growth_row["country"])
        cities = cities_by_country.get(country, [])
        if not cities:
            raise ValueError(f"No representative cities found for country {country!r}.")
        growth_mw = float(growth_row["growth_mw"])
        for city in cities:
            for scale_definition in scale_definitions:
                scale_capacity_mw = growth_mw * scale_definition.ratio
                split = choose_facility_count(
                    total_mw=scale_capacity_mw,
                    min_mw=scale_definition.min_capacity_mw,
                    max_mw=scale_definition.max_capacity_mw,
                )
                rows.append(
                    {
                        "country": country,
                        "growth_scenario": growth_row["growth_scenario"],
                        "city": city,
                        "city_count_in_country": len(cities),
                        "country_growth_mw": growth_mw,
                        "city_growth_mw": growth_mw,
                        "scale": scale_definition.scale,
                        "scale_share": scale_definition.ratio,
                        "scale_min_capacity_mw": scale_definition.min_capacity_mw,
                        "scale_max_capacity_mw": scale_definition.max_capacity_mw,
                        "scale_capacity_mw": scale_capacity_mw,
                        "facility_count": split.facility_count,
                        "facility_capacity_mw": split.facility_capacity_mw,
                        "allocated_capacity_mw": split.facility_count * split.facility_capacity_mw,
                        "below_scale_min": split.below_scale_min,
                    }
                )
    return pd.DataFrame(rows)


def choose_facility_count(total_mw: float, min_mw: float, max_mw: float) -> FacilitySplit:
    """Split total MW into facilities as close as possible to the scale midpoint."""
    total = float(total_mw)
    minimum = float(min_mw)
    maximum = float(max_mw)
    if total < 0:
        raise ValueError("total_mw must be non-negative.")
    if minimum <= 0 or maximum <= 0 or minimum > maximum:
        raise ValueError("min_mw and max_mw must be positive and min_mw <= max_mw.")
    if math.isclose(total, 0.0):
        return FacilitySplit(0, 0.0, False)
    if total < minimum:
        return FacilitySplit(1, total, True)

    feasible_n_min = max(1, math.ceil(total / maximum))
    feasible_n_max = max(1, math.floor(total / minimum))
    if feasible_n_min > feasible_n_max:
        return FacilitySplit(1, total, total < minimum)

    midpoint = (minimum + maximum) / 2.0
    target_n = total / midpoint
    candidates = {feasible_n_min, feasible_n_max}
    for candidate in (math.floor(target_n), math.ceil(target_n)):
        for offset in (-1, 0, 1):
            value = int(candidate + offset)
            if feasible_n_min <= value <= feasible_n_max:
                candidates.add(value)
    facility_count = min(
        candidates,
        key=lambda n: (abs(total / n - midpoint), abs(n - target_n), n),
    )
    return FacilitySplit(facility_count, total / facility_count, False)


def run_cooling_comparisons(
    *,
    allocations: pd.DataFrame,
    workload_file: str | Path,
    idle_power_fraction: float,
    hours: int | None,
    start_time: str | None,
    time_alignment: str | None,
    max_carbon_gap_hours: int,
    hub_height_m: float,
    wind_loss_fraction: float,
    wind_cut_in: float,
    wind_rated: float,
    wind_cut_out: float,
    energy_cache: dict[tuple[object, ...], DataCenterEnergyResult],
    wind_resource_cache: dict[tuple[object, ...], WindResourceResult],
    energy_calculator: EnergyCalculator,
    wind_calculator: WindCalculator,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    total_rows = len(allocations)
    for index, allocation in enumerate(allocations.to_dict(orient="records"), start=1):
        for cooling_type in COOLING_TYPES:
            print(
                "Cooling "
                f"{index}/{total_rows}: {allocation['country']} / {allocation['growth_scenario']} / "
                f"{allocation['city']} / {allocation['scale']} / {cooling_type}",
                flush=True,
            )
            row = _base_result_row(allocation)
            row["cooling_type"] = cooling_type
            try:
                if int(allocation["facility_count"]) == 0:
                    rows.append(_zero_cooling_row(row, hours))
                    continue
                rated_it_power_kw = float(allocation["facility_capacity_mw"]) * 1000.0
                energy = _get_energy_result(
                    cache=energy_cache,
                    energy_calculator=energy_calculator,
                    city=str(allocation["city"]),
                    cooling_type=cooling_type,
                    workload_file=workload_file,
                    rated_it_power_kw=rated_it_power_kw,
                    idle_power_fraction=idle_power_fraction,
                    hours=hours,
                    start_time=start_time,
                    time_alignment=time_alignment,
                    max_carbon_gap_hours=max_carbon_gap_hours,
                )
                wind_resource = _get_wind_resource(
                    cache=wind_resource_cache,
                    wind_calculator=wind_calculator,
                    city=str(allocation["city"]),
                    hub_height_m=hub_height_m,
                    loss_fraction=wind_loss_fraction,
                    cut_in=wind_cut_in,
                    rated=wind_rated,
                    cut_out=wind_cut_out,
                )
                rows.append(
                    _cooling_result_row(
                        row=row,
                        energy=energy,
                        wind_resource=wind_resource,
                        facility_count=int(allocation["facility_count"]),
                    )
                )
            except Exception as exc:
                rows.append(_failed_row(row, str(exc)))
    return pd.DataFrame(rows)


def run_optimization_comparisons(
    *,
    allocations: pd.DataFrame,
    cooling: str,
    objectives: tuple[str, ...],
    workload_file: str | Path,
    idle_power_fraction: float,
    hours: int | None,
    start_time: str | None,
    time_alignment: str | None,
    max_carbon_gap_hours: int,
    battery_capacity_mwh: float,
    battery_roundtrip_efficiency: float,
    grid_import_limit_mw: float | None,
    battery_charge_limit_mw: float | None,
    battery_discharge_limit_mw: float | None,
    load_shift_fraction: float,
    hub_height_m: float,
    wind_loss_fraction: float,
    wind_cut_in: float,
    wind_rated: float,
    wind_cut_out: float,
    energy_cache: dict[tuple[object, ...], DataCenterEnergyResult],
    wind_resource_cache: dict[tuple[object, ...], WindResourceResult],
    required_wind_cache: dict[tuple[object, ...], RequiredWindCapacity],
    energy_calculator: EnergyCalculator,
    wind_calculator: WindCalculator,
    optimizer: Optimizer,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    total_rows = len(allocations)
    scenario_configs = _scenario_configs(
        cooling=cooling,
        load_shift_fraction=load_shift_fraction,
        battery_capacity_mwh=battery_capacity_mwh,
        battery_charge_limit_mw=battery_charge_limit_mw,
        battery_discharge_limit_mw=battery_discharge_limit_mw,
    )
    for index, allocation in enumerate(allocations.to_dict(orient="records"), start=1):
        for scenario_config in scenario_configs:
            optimization_scenario = str(scenario_config["scenario"])
            scenario_cooling = str(scenario_config["cooling_type"])
            for objective in objectives:
                print(
                    "Optimization "
                    f"{index}/{total_rows}: {allocation['country']} / {allocation['growth_scenario']} / "
                    f"{allocation['city']} / {allocation['scale']} / {optimization_scenario} / {objective}",
                    flush=True,
                )
                row = _base_result_row(allocation)
                row.update(
                    {
                        "objective": objective,
                        "optimization_scenario": optimization_scenario,
                        "optimization_scenario_label": OPTIMIZATION_SCENARIO_LABELS.get(
                            optimization_scenario,
                            optimization_scenario,
                        ),
                        "cooling_type": scenario_cooling,
                        "load_shift_enabled": bool(scenario_config["load_shift_enabled"]),
                        "battery_enabled": bool(scenario_config["battery_enabled"]),
                        "configured_load_shift_fraction": float(scenario_config["load_shift_fraction"]),
                    }
                )
                try:
                    if int(allocation["facility_count"]) == 0:
                        rows.append(_zero_optimization_row(row, hours))
                        continue
                    rated_it_power_kw = float(allocation["facility_capacity_mw"]) * 1000.0
                    wind_capacity = _get_required_wind_capacity(
                        cache=required_wind_cache,
                        energy_cache=energy_cache,
                        wind_resource_cache=wind_resource_cache,
                        energy_calculator=energy_calculator,
                        wind_calculator=wind_calculator,
                        city=str(allocation["city"]),
                        cooling_type=scenario_cooling,
                        workload_file=workload_file,
                        rated_it_power_kw=rated_it_power_kw,
                        idle_power_fraction=idle_power_fraction,
                        hours=hours,
                        start_time=start_time,
                        time_alignment=time_alignment,
                        max_carbon_gap_hours=max_carbon_gap_hours,
                        hub_height_m=hub_height_m,
                        wind_loss_fraction=wind_loss_fraction,
                        wind_cut_in=wind_cut_in,
                        wind_rated=wind_rated,
                        wind_cut_out=wind_cut_out,
                    )
                    result = optimizer(
                        city=str(allocation["city"]),
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
                    rows.append(
                        _optimization_result_row(
                            row=row,
                            wind_capacity=wind_capacity,
                            result=result,
                            facility_count=int(allocation["facility_count"]),
                        )
                    )
                except Exception as exc:
                    rows.append(_failed_row(row, str(exc)))
    return pd.DataFrame(rows)


def append_scale_totals(
    city_scale_results: pd.DataFrame,
    metric_columns: list[str],
    *,
    extra_group_columns: list[str],
) -> pd.DataFrame:
    """Append all-scale city totals while preserving scale-level rows."""
    if city_scale_results.empty:
        return city_scale_results.copy()

    group_columns = ["country", "growth_scenario", "city", *extra_group_columns]
    total_rows: list[dict[str, object]] = []
    for _, group in city_scale_results.groupby(group_columns, dropna=False, sort=True):
        first = group.iloc[0].to_dict()
        total_row = {column: first.get(column) for column in group_columns}
        total_row.update(
            {
                "scale": "all_scales",
                "city_count_in_country": first.get("city_count_in_country"),
                "country_growth_mw": first.get("country_growth_mw"),
                "city_growth_mw": first.get("city_growth_mw"),
                "scale_share": 1.0,
                "scale_capacity_mw": _numeric_sum(group, "scale_capacity_mw"),
                "facility_count": int(_numeric_sum(group, "facility_count")),
                "facility_capacity_mw": math.nan,
                "below_scale_min": bool(group["below_scale_min"].fillna(False).astype(bool).any()),
                "status": _combined_status(group),
                "error_message": _combine_errors(group),
            }
        )
        for metric in metric_columns:
            total_row[metric] = _aggregate_city_metric(group, metric)
        total_rows.append(total_row)

    return pd.concat([city_scale_results, pd.DataFrame(total_rows)], ignore_index=True, sort=False)


def build_country_average_results(
    city_results: pd.DataFrame,
    *,
    metric_columns: list[str],
    extra_group_columns: list[str],
) -> pd.DataFrame:
    """Average city results within each country, scenario, and comparison group."""
    if city_results.empty:
        return city_results.copy()
    group_columns = ["country", "growth_scenario", *extra_group_columns]
    rows: list[dict[str, object]] = []
    for _, group in city_results.groupby(group_columns, dropna=False, sort=True):
        first = group.iloc[0].to_dict()
        city_count = int(group["city"].nunique()) if "city" in group else len(group)
        row = {column: first.get(column) for column in group_columns}
        row.update(
            {
                "representative_city_count": city_count,
                "country_growth_mw": first.get("country_growth_mw"),
                "average_city_growth_mw": _numeric_mean(group, "city_growth_mw"),
                "scale": first.get("scale"),
                "scale_share": first.get("scale_share"),
                "average_scale_capacity_mw": _numeric_mean(group, "scale_capacity_mw"),
                "average_facility_count": _numeric_mean(group, "facility_count"),
                "average_facility_capacity_mw": _numeric_mean(group, "facility_capacity_mw"),
                "below_scale_min_city_count": int(group["below_scale_min"].fillna(False).astype(bool).sum())
                if "below_scale_min" in group
                else 0,
                "status": _combined_status(group),
                "error_message": _combine_errors(group),
            }
        )
        for metric in metric_columns:
            row[metric] = _aggregate_country_metric(group, metric)
        rows.append(row)
    return pd.DataFrame(rows)


def _get_energy_result(
    *,
    cache: dict[tuple[object, ...], DataCenterEnergyResult],
    energy_calculator: EnergyCalculator,
    city: str,
    cooling_type: str,
    workload_file: str | Path,
    rated_it_power_kw: float,
    idle_power_fraction: float,
    hours: int | None,
    start_time: str | None,
    time_alignment: str | None,
    max_carbon_gap_hours: int,
) -> DataCenterEnergyResult:
    key = (
        city,
        cooling_type,
        round(float(rated_it_power_kw), 9),
        hours,
        start_time,
        time_alignment,
        str(workload_file),
        round(float(idle_power_fraction), 9),
        max_carbon_gap_hours,
    )
    if key not in cache:
        cache[key] = energy_calculator(
            city=city,
            cooling_type=cooling_type,
            workload_file=workload_file,
            rated_it_power_kw=rated_it_power_kw,
            idle_power_fraction=idle_power_fraction,
            hours=hours,
            start_time=start_time,
            time_alignment=time_alignment,
            max_carbon_gap_hours=max_carbon_gap_hours,
            progress=False,
        )
    return cache[key]


def _get_wind_resource(
    *,
    cache: dict[tuple[object, ...], WindResourceResult],
    wind_calculator: WindCalculator,
    city: str,
    hub_height_m: float,
    loss_fraction: float,
    cut_in: float,
    rated: float,
    cut_out: float,
) -> WindResourceResult:
    key = (
        city,
        round(float(hub_height_m), 9),
        round(float(loss_fraction), 9),
        round(float(cut_in), 9),
        round(float(rated), 9),
        round(float(cut_out), 9),
    )
    if key not in cache:
        cache[key] = wind_calculator(
            city=city,
            hub_height_m=hub_height_m,
            loss_fraction=loss_fraction,
            cut_in=cut_in,
            rated=rated,
            cut_out=cut_out,
        )
    return cache[key]


def _get_required_wind_capacity(
    *,
    cache: dict[tuple[object, ...], RequiredWindCapacity],
    energy_cache: dict[tuple[object, ...], DataCenterEnergyResult],
    wind_resource_cache: dict[tuple[object, ...], WindResourceResult],
    energy_calculator: EnergyCalculator,
    wind_calculator: WindCalculator,
    city: str,
    cooling_type: str,
    workload_file: str | Path,
    rated_it_power_kw: float,
    idle_power_fraction: float,
    hours: int | None,
    start_time: str | None,
    time_alignment: str | None,
    max_carbon_gap_hours: int,
    hub_height_m: float,
    wind_loss_fraction: float,
    wind_cut_in: float,
    wind_rated: float,
    wind_cut_out: float,
) -> RequiredWindCapacity:
    key = (
        city,
        cooling_type,
        round(float(rated_it_power_kw), 9),
        hours,
        start_time,
        time_alignment,
        max_carbon_gap_hours,
        round(float(hub_height_m), 9),
        round(float(wind_loss_fraction), 9),
        round(float(wind_cut_in), 9),
        round(float(wind_rated), 9),
        round(float(wind_cut_out), 9),
    )
    if key not in cache:
        energy = _get_energy_result(
            cache=energy_cache,
            energy_calculator=energy_calculator,
            city=city,
            cooling_type=cooling_type,
            workload_file=workload_file,
            rated_it_power_kw=rated_it_power_kw,
            idle_power_fraction=idle_power_fraction,
            hours=hours,
            start_time=start_time,
            time_alignment=time_alignment,
            max_carbon_gap_hours=max_carbon_gap_hours,
        )
        wind_resource = _get_wind_resource(
            cache=wind_resource_cache,
            wind_calculator=wind_calculator,
            city=city,
            hub_height_m=hub_height_m,
            loss_fraction=wind_loss_fraction,
            cut_in=wind_cut_in,
            rated=wind_rated,
            cut_out=wind_cut_out,
        )
        datacenter_total_energy_mwh = energy.total_energy_kwh / 1000.0
        required_wind_capacity_mw = datacenter_total_energy_mwh / wind_resource.wind_generation_per_mw_mwh
        cache[key] = RequiredWindCapacity(
            city=city,
            cooling_type=cooling_type,
            rated_it_power_kw=float(rated_it_power_kw),
            hours=energy.hours,
            datacenter_total_energy_mwh=datacenter_total_energy_mwh,
            required_wind_capacity_mw=required_wind_capacity_mw,
            wind_generation_per_mw_mwh=wind_resource.wind_generation_per_mw_mwh,
            mean_net_capacity_factor=wind_resource.mean_net_capacity_factor,
            point_id=wind_resource.point_id,
            wind_nc_file=wind_resource.wind_nc_file,
            wind_start_time=wind_resource.wind_start_time,
            wind_end_time=wind_resource.wind_end_time,
        )
    return cache[key]


def _cooling_result_row(
    *,
    row: dict[str, object],
    energy: DataCenterEnergyResult,
    wind_resource: WindResourceResult,
    facility_count: int,
) -> dict[str, object]:
    multiplier = float(facility_count)
    total_energy_mwh_per_facility = energy.total_energy_kwh / 1000.0
    required_wind_capacity_mw_per_facility = (
        total_energy_mwh_per_facility / wind_resource.wind_generation_per_mw_mwh
    )
    row.update(
        {
            "status": "ok",
            "error_message": "",
            "hours": energy.hours,
            "simulation_start_time": energy.simulation_start_time,
            "simulation_end_time": energy.simulation_end_time,
            "time_alignment": energy.time_alignment,
            "rated_it_power_kw_per_facility": energy.rated_it_power_kw,
            "server_energy_kwh": energy.it_energy_kwh * multiplier,
            "server_carbon_emissions_kgco2": energy.it_carbon_emissions_kgco2 * multiplier,
            "cooling_energy_kwh": energy.cooling_energy_kwh * multiplier,
            "cooling_carbon_emissions_kgco2": energy.cooling_carbon_emissions_kgco2 * multiplier,
            "total_energy_kwh": energy.total_energy_kwh * multiplier,
            "total_carbon_emissions_kgco2": energy.carbon_emissions_kgco2 * multiplier,
            "required_wind_capacity_mw": required_wind_capacity_mw_per_facility * multiplier,
            "wind_annual_generation_mwh": total_energy_mwh_per_facility * multiplier,
            "wind_generation_per_mw_mwh": wind_resource.wind_generation_per_mw_mwh,
            "wind_mean_net_capacity_factor": wind_resource.mean_net_capacity_factor,
            "wind_point_id": wind_resource.point_id,
            "wind_nc_file": wind_resource.wind_nc_file,
            "wind_start_time": wind_resource.wind_start_time,
            "wind_end_time": wind_resource.wind_end_time,
        }
    )
    return row


def _optimization_result_row(
    *,
    row: dict[str, object],
    wind_capacity: RequiredWindCapacity,
    result: dict[str, object],
    facility_count: int,
) -> dict[str, object]:
    multiplier = float(facility_count)
    clean_result = {
        key: value
        for key, value in result.items()
        if key not in OPTIMIZATION_HOURLY_RESULT_KEYS and key != "csv_files"
    }
    row.update(
        {
            "status": "ok",
            "error_message": "",
            "hours": wind_capacity.hours,
            "rated_it_power_kw_per_facility": wind_capacity.rated_it_power_kw,
            "point_id": wind_capacity.point_id,
            "wind_nc_file": wind_capacity.wind_nc_file,
            "wind_generation_per_mw_mwh": wind_capacity.wind_generation_per_mw_mwh,
            "wind_mean_net_capacity_factor": wind_capacity.mean_net_capacity_factor,
            "wind_start_time": wind_capacity.wind_start_time,
            "wind_end_time": wind_capacity.wind_end_time,
        }
    )
    row.update(clean_result)
    row["required_wind_capacity_mw"] = wind_capacity.required_wind_capacity_mw
    row["datacenter_total_energy_mwh"] = wind_capacity.datacenter_total_energy_mwh
    _scale_optimization_metrics(row, multiplier)
    if "wind_coverage_mwh" not in row or pd.isna(row.get("wind_coverage_mwh")):
        demand = float(row.get("annual_demand_mwh", row.get("datacenter_total_energy_mwh", 0.0)) or 0.0)
        grid = float(row.get("grid_purchase_mwh", 0.0) or 0.0)
        row["wind_coverage_mwh"] = demand - grid
    if "battery_required_capacity_mwh" not in row:
        row["battery_required_capacity_mwh"] = 0.0
    return row


def _scale_optimization_metrics(row: dict[str, object], multiplier: float) -> None:
    average_metrics = {
        "average_grid_carbon_intensity_g_per_kwh",
        "renewable_physical_coverage_fraction",
        "load_movement_budget_used_fraction",
    }
    for metric in OPTIMIZATION_RESULT_METRICS:
        if metric in row and metric not in average_metrics:
            try:
                row[metric] = float(row[metric]) * multiplier
            except Exception:
                pass


def _base_result_row(allocation: dict[str, object]) -> dict[str, object]:
    return {column: allocation.get(column) for column in BASE_METADATA_COLUMNS}


def _zero_cooling_row(row: dict[str, object], hours: int | None) -> dict[str, object]:
    row.update({"status": "ok", "error_message": "", "hours": hours})
    for metric in COOLING_METRICS:
        row[metric] = 0.0
    return row


def _zero_optimization_row(row: dict[str, object], hours: int | None) -> dict[str, object]:
    row.update({"status": "ok", "error_message": "", "hours": hours})
    for metric in OPTIMIZATION_RESULT_METRICS:
        row[metric] = 0.0
    return row


def _failed_row(row: dict[str, object], error_message: str) -> dict[str, object]:
    row["status"] = "failed"
    row["error_message"] = error_message
    return row


def _aggregate_city_metric(group: pd.DataFrame, metric: str) -> float:
    if metric not in group:
        return math.nan
    values = pd.to_numeric(group[metric], errors="coerce")
    if metric in {
        "average_grid_carbon_intensity_g_per_kwh",
        "renewable_physical_coverage_fraction",
        "load_movement_budget_used_fraction",
    }:
        return float(values.mean()) if values.notna().any() else math.nan
    return float(values.sum()) if values.notna().any() else math.nan


def _aggregate_country_metric(group: pd.DataFrame, metric: str) -> float:
    if metric not in group:
        return math.nan
    values = pd.to_numeric(group[metric], errors="coerce")
    return float(values.mean()) if values.notna().any() else math.nan


def _numeric_sum(group: pd.DataFrame, column: str) -> float:
    if column not in group:
        return 0.0
    return float(pd.to_numeric(group[column], errors="coerce").fillna(0.0).sum())


def _numeric_mean(group: pd.DataFrame, column: str) -> float:
    if column not in group:
        return math.nan
    values = pd.to_numeric(group[column], errors="coerce")
    return float(values.mean()) if values.notna().any() else math.nan


def _combined_status(group: pd.DataFrame) -> str:
    if "status" not in group:
        return ""
    statuses = set(group["status"].dropna().astype(str))
    if statuses == {"ok"}:
        return "ok"
    if "failed" in statuses:
        return "failed"
    return "|".join(sorted(statuses))


def _combine_errors(group: pd.DataFrame) -> str:
    if "error_message" not in group:
        return ""
    errors = sorted({str(error) for error in group["error_message"].dropna() if str(error).strip()})
    return "; ".join(errors)


def _write_foundation_outputs(
    output_path: Path,
    country_growths: pd.DataFrame,
    city_scale_allocations: pd.DataFrame,
) -> dict[str, Path]:
    files = {
        "country_growths_csv": output_path / "country_growths.csv",
        "city_scale_allocations_csv": output_path / "city_scale_allocations.csv",
    }
    country_growths.to_csv(files["country_growths_csv"], index=False, encoding="utf-8-sig")
    city_scale_allocations.to_csv(files["city_scale_allocations_csv"], index=False, encoding="utf-8-sig")
    for label, path in files.items():
        print(f"{label}: {path}")
    return files


def _cities_by_country(
    city_rows: list[dict[str, object]],
    *,
    include_not_ready: bool,
) -> dict[str, list[str]]:
    if not city_rows:
        raise ValueError(f"Workbook sheet {CITY_MANIFEST_SHEET} is empty.")
    columns = list(city_rows[0].keys())
    country_column = _find_column(columns, ["country", "country_area", "nation"], "city country")
    city_column = _find_column(columns, ["datacentermap_market", "city", "market"], "city")
    ready_column = _find_optional_column(columns, ["toolkit_ready", "ready"])
    cities: dict[str, list[str]] = {}
    for row in city_rows:
        if not include_not_ready and ready_column and not _is_ready(row.get(ready_column)):
            continue
        country = _text(row.get(country_column))
        city = _text(row.get(city_column))
        if not country or not city:
            continue
        cities.setdefault(country, [])
        if city not in cities[country]:
            cities[country].append(city)
    return cities


def _find_column(columns: list[str], candidates: list[str], label: str) -> str:
    normalized = {_normalize_column(column): column for column in columns}
    for candidate in candidates:
        if _normalize_column(candidate) in normalized:
            return normalized[_normalize_column(candidate)]
    raise ValueError(
        f"Could not identify {label} column. Available columns: {', '.join(map(str, columns))}"
    )


def _find_optional_column(columns: list[str], candidates: list[str]) -> str | None:
    normalized = {_normalize_column(column): column for column in columns}
    for candidate in candidates:
        if _normalize_column(candidate) in normalized:
            return normalized[_normalize_column(candidate)]
    return None


def _find_2025_capacity_column(columns: list[str]) -> str:
    candidates = [
        column
        for column in columns
        if "2025" in _normalize_column(column) and _capacity_unit_from_column(column) in {"mw", "gw"}
    ]
    if len(candidates) != 1:
        raise ValueError(
            "Could not uniquely identify the 2025 baseline capacity column. "
            f"Candidates: {candidates}; available columns: {columns}"
        )
    return candidates[0]


def _find_2030_capacity_columns(columns: list[str]) -> list[str]:
    candidates = [
        column
        for column in columns
        if "2030" in _normalize_column(column) and _capacity_unit_from_column(column) in {"mw", "gw"}
    ]
    if len(candidates) != 4:
        raise ValueError(
            "Expected exactly four 2030 scenario capacity columns. "
            f"Found {len(candidates)}: {candidates}"
        )
    return candidates


def _capacity_to_mw(value: object, column_name: str) -> float:
    amount = _number(value, column_name)
    unit = _capacity_unit_from_column(column_name)
    if unit == "gw":
        return amount * 1000.0
    if unit == "mw":
        return amount
    raise ValueError(f"Could not identify MW/GW unit from capacity column {column_name!r}.")


def _capacity_unit_from_column(column_name: str) -> str:
    normalized = _normalize_column(column_name)
    tokens = set(normalized.split("_"))
    if "gw" in tokens:
        return "gw"
    if "mw" in tokens:
        return "mw"
    return ""


def _scenario_label_from_column(column_name: str) -> str:
    label = re.sub(r"(?i).*2030[_\s-]*", "", str(column_name)).strip("_ -")
    return label or str(column_name)


def _normalize_scale(value: object) -> str:
    raw = _text(value).lower()
    aliases = {
        "small": "small",
        "s": "small",
        "small_scale": "small",
        "小": "small",
        "中": "medium",
        "medium": "medium",
        "m": "medium",
        "medium_scale": "medium",
        "large": "large",
        "l": "large",
        "large_scale": "large",
        "大": "large",
    }
    normalized = raw.replace("-", "_").replace(" ", "_")
    if normalized in aliases:
        return aliases[normalized]
    raise ValueError(f"Unknown data-center scale value: {value!r}")


def _number(value: object, label: str) -> float:
    try:
        number = float(value)
    except Exception as exc:
        raise ValueError(f"{label} must be numeric; got {value!r}.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite; got {value!r}.")
    return number


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _is_ready(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _normalize_column(value: object) -> str:
    token = str(value).replace("\ufeff", "").strip().lower()
    token = re.sub(r"[^a-z0-9]+", "_", token)
    return token.strip("_")


def _resolve_path(path: str | Path) -> Path:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = ROOT_DIR / resolved
    return resolved


def _resolve_output_dir(path: str | Path) -> Path:
    output_path = _resolve_path(path)
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path


def _baseline_time_alignment(start_time: str | None, time_alignment: str | None) -> str | None:
    if start_time:
        return "start_time"
    if time_alignment in (None, "sst"):
        return "sst"
    return time_alignment


def _hours_token(hours: int | None) -> str:
    return "all_hours" if hours is None else f"{hours}h"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Allocate country 2030 growth to representative cities and compare cooling/optimization scenarios."
    )
    parser.add_argument("--manifest-file", default=str(CITY_MAP_FILE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--include-not-ready", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workload-file", default=str(WORKLOAD_FILE))
    parser.add_argument("--idle-power-fraction", type=float, default=0.3)
    parser.add_argument("--hours", type=int, default=8760)
    parser.add_argument("--start-time", default="2025-01-01 00:00")
    parser.add_argument("--time-alignment", choices=["sst", "latest", "start_time"], default=None)
    parser.add_argument("--max-carbon-gap-hours", type=int, default=6)
    parser.add_argument("--cooling", choices=["seawater", "air_source"], default="seawater")
    parser.add_argument("--objectives", nargs="+", default=["min-grid-mwh", "min-grid-co2"])
    parser.add_argument("--battery-capacity-mwh", type=float, default=535.4)
    parser.add_argument("--battery-roundtrip-efficiency", type=float, default=0.97)
    parser.add_argument("--grid-import-limit-mw", type=float, default=None)
    parser.add_argument("--battery-charge-limit-mw", type=float, default=25.0)
    parser.add_argument("--battery-discharge-limit-mw", type=float, default=25.0)
    parser.add_argument("--load-shift-fraction", type=float, default=0.3)
    parser.add_argument("--hub-height-m", type=float, default=150.0)
    parser.add_argument("--wind-loss-fraction", type=float, default=0.15)
    parser.add_argument("--wind-cut-in", type=float, default=3.0)
    parser.add_argument("--wind-rated", type=float, default=12.0)
    parser.add_argument("--wind-cut-out", type=float, default=25.0)
    args = parser.parse_args(argv)

    output_files = run_country_growth_allocation(
        manifest_file=args.manifest_file,
        output_dir=args.output_dir,
        include_not_ready=args.include_not_ready,
        dry_run=args.dry_run,
        workload_file=args.workload_file,
        idle_power_fraction=args.idle_power_fraction,
        hours=args.hours,
        start_time=args.start_time,
        time_alignment=args.time_alignment,
        max_carbon_gap_hours=args.max_carbon_gap_hours,
        cooling=args.cooling,
        objectives=tuple(args.objectives),
        battery_capacity_mwh=args.battery_capacity_mwh,
        battery_roundtrip_efficiency=args.battery_roundtrip_efficiency,
        grid_import_limit_mw=args.grid_import_limit_mw,
        battery_charge_limit_mw=args.battery_charge_limit_mw,
        battery_discharge_limit_mw=args.battery_discharge_limit_mw,
        load_shift_fraction=args.load_shift_fraction,
        hub_height_m=args.hub_height_m,
        wind_loss_fraction=args.wind_loss_fraction,
        wind_cut_in=args.wind_cut_in,
        wind_rated=args.wind_rated,
        wind_cut_out=args.wind_cut_out,
    )
    print({key: str(path) for key, path in output_files.items()})


if __name__ == "__main__":
    main(sys.argv[1:])
