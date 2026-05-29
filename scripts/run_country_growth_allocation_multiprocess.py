"""Country growth allocation runner for cooling and optimization comparisons.

The runner reads the country, city, and data-center scale sheets from
data/coastal_datacenter_city_manifest.xlsx. For each country and 2030 scenario,
every toolkit-ready representative city carries the coastal portion of the
country's growth capacity, calculated from Country_manifest
coastal_share_of_total_pct. The city capacity is split across small, medium,
and large data-center scales before running cooling and dispatch comparisons.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

from utils.tools import (_resolve_baseline_alignment, _resolve_path, _pct, _resolve_output_dir, _hours_token, _number, _row_numeric_value,
                         _numeric_sum, _numeric_mean, _text, _is_ready, _normalize_column, _row_value, _find_column, _capacity_to_mw,
                         _capacity_unit_from_column, _scenario_label_from_column)

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

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
COOLING_DIAGNOSTIC_COLUMNS = [
    "unmet_cooling_energy_kwh",
    "constraint_violation_hours",
    "outfall_temperature_violation_hours",
    "seawater_temperature_violation_hours",
    "max_seawater_flow_rate_m3_s",
    "max_seawater_heat_exchange_unit_count",
    "model_warning_count",
    "model_warning_messages",
]
COOLING_ISSUE_COLUMNS = [
    "severity",
    "issue_type",
    "country",
    "growth_scenario",
    "city",
    "scale",
    "cooling_type",
    "hours",
    "affected_hours",
    "metric_value",
    "metric_unit",
    "facility_count",
    "facility_capacity_mw",
    "message",
    "error_message",
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


@dataclass(frozen=True)
class CoolingTaskContext:
    total_tasks: int
    workload_file: str | Path
    idle_power_fraction: float
    hours: int | None
    start_time: str | None
    time_alignment: str | None
    max_carbon_gap_hours: int
    hub_height_m: float
    wind_loss_fraction: float
    wind_cut_in: float
    wind_rated: float
    wind_cut_out: float
    energy_calculator: "EnergyCalculator"
    wind_calculator: "WindCalculator"


@dataclass(frozen=True)
class OptimizationTaskContext:
    total_tasks: int
    cooling: str
    objectives: tuple[str, ...]
    workload_file: str | Path
    idle_power_fraction: float
    hours: int | None
    start_time: str | None
    time_alignment: str | None
    max_carbon_gap_hours: int
    battery_roundtrip_efficiency: float
    grid_import_limit_mw: float | None
    hub_height_m: float
    wind_loss_fraction: float
    wind_cut_in: float
    wind_rated: float
    wind_cut_out: float
    energy_calculator: "EnergyCalculator"
    wind_calculator: "WindCalculator"
    optimizer: "Optimizer"


EnergyCalculator = Callable[..., DataCenterEnergyResult]
WindCalculator = Callable[..., WindResourceResult]
Optimizer = Callable[..., dict[str, object]]


def run_country_growth_allocation(
    *,
    manifest_file: str | Path = CITY_MAP_FILE,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    include_not_ready: bool = False,
    dry_run: bool = False,
    mode: str = "all",
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
    workers: int = 1,
    countries: Iterable[str] | None = None,
    max_countries: int | None = None,
    country_rows: list[dict[str, object]] | None = None,
    city_rows: list[dict[str, object]] | None = None,
    scale_rows: list[dict[str, object]] | None = None,
    energy_calculator: EnergyCalculator = calculate_data_center_energy,
    wind_calculator: WindCalculator = calculate_wind_resource,
    optimizer: Optimizer = optimization,
    write_debug_scale_results: bool = False,
) -> dict[str, Path]:
    """Run country-growth allocation and write CSV outputs."""
    run_mode = _normalize_mode(mode)
    manifest_path = _resolve_path(manifest_file)
    output_path = _resolve_output_dir(output_dir)
    worker_count = _normalize_workers(workers)
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
    country_growths, city_scale_allocations = _filter_country_growth_inputs(
        country_growths=country_growths,
        city_scale_allocations=city_scale_allocations,
        countries=countries,
        max_countries=max_countries,
    )

    output_files = _write_foundation_outputs(output_path, country_growths, city_scale_allocations)
    if dry_run:
        print(f"Dry run complete. Foundation CSVs written under {output_path}")
        return output_files

    energy_cache: dict[tuple[object, ...], DataCenterEnergyResult] = {}
    wind_resource_cache: dict[tuple[object, ...], WindResourceResult] = {}
    required_wind_cache: dict[tuple[object, ...], RequiredWindCapacity] = {}
    energy_cache_locks: dict[tuple[object, ...], threading.Lock] = {}
    wind_resource_cache_locks: dict[tuple[object, ...], threading.Lock] = {}
    required_wind_cache_locks: dict[tuple[object, ...], threading.Lock] = {}
    cache_locks_guard = threading.Lock()

    if run_mode in {"all", "cooling"}:
        output_files.update(
            _run_country_growth_cooling_outputs(
                output_path=output_path,
                allocations=city_scale_allocations,
                workload_file=workload_file,
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
                energy_cache=energy_cache,
                wind_resource_cache=wind_resource_cache,
                energy_cache_locks=energy_cache_locks,
                wind_resource_cache_locks=wind_resource_cache_locks,
                cache_locks_guard=cache_locks_guard,
                energy_calculator=energy_calculator,
                wind_calculator=wind_calculator,
                workers=worker_count,
                write_debug_scale_results=write_debug_scale_results,
            )
        )

    if run_mode in {"all", "load-shift"}:
        output_files.update(
            _run_country_growth_load_shift_outputs(
                output_path=output_path,
                allocations=city_scale_allocations,
                cooling=cooling,
                objectives=tuple(objectives),
                workload_file=workload_file,
                idle_power_fraction=idle_power_fraction,
                hours=hours,
                start_time=start_time,
                time_alignment=time_alignment,
                max_carbon_gap_hours=max_carbon_gap_hours,
                battery_roundtrip_efficiency=battery_roundtrip_efficiency,
                grid_import_limit_mw=grid_import_limit_mw,
                load_shift_fraction=load_shift_fraction,
                hub_height_m=hub_height_m,
                wind_loss_fraction=wind_loss_fraction,
                wind_cut_in=wind_cut_in,
                wind_rated=wind_rated,
                wind_cut_out=wind_cut_out,
                energy_cache=energy_cache,
                wind_resource_cache=wind_resource_cache,
                required_wind_cache=required_wind_cache,
                energy_cache_locks=energy_cache_locks,
                wind_resource_cache_locks=wind_resource_cache_locks,
                required_wind_cache_locks=required_wind_cache_locks,
                cache_locks_guard=cache_locks_guard,
                energy_calculator=energy_calculator,
                wind_calculator=wind_calculator,
                optimizer=optimizer,
                workers=worker_count,
                write_debug_scale_results=write_debug_scale_results,
            )
        )

    for label, path in output_files.items():
        print(f"{label}: {path}")
    return output_files


def run_country_growth_cooling_comparison(
    *,
    manifest_file: str | Path = CITY_MAP_FILE,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    include_not_ready: bool = False,
    workload_file: str | Path = WORKLOAD_FILE,
    idle_power_fraction: float = 0.35,
    hours: int | None = 8760,
    start_time: str | None = "2025-01-01 00:00",
    time_alignment: str | None = None,
    max_carbon_gap_hours: int = 6,
    hub_height_m: float = 150.0,
    wind_loss_fraction: float = 0.15,
    wind_cut_in: float = 3.0,
    wind_rated: float = 12.0,
    wind_cut_out: float = 25.0,
    workers: int = 1,
    countries: Iterable[str] | None = None,
    max_countries: int | None = None,
    country_rows: list[dict[str, object]] | None = None,
    city_rows: list[dict[str, object]] | None = None,
    scale_rows: list[dict[str, object]] | None = None,
    energy_calculator: EnergyCalculator = calculate_data_center_energy,
    wind_calculator: WindCalculator = calculate_wind_resource,
    write_debug_scale_results: bool = False,
) -> dict[str, Path]:
    """Write city/country summaries comparing seawater cooling against air-source cooling."""
    output_path, country_growths, city_scale_allocations = _prepare_country_growth_inputs(
        manifest_file=manifest_file,
        output_dir=output_dir,
        include_not_ready=include_not_ready,
        country_rows=country_rows,
        city_rows=city_rows,
        scale_rows=scale_rows,
        countries=countries,
        max_countries=max_countries,
    )
    output_files = _write_foundation_outputs(output_path, country_growths, city_scale_allocations)
    cache_locks_guard = threading.Lock()
    output_files.update(
        _run_country_growth_cooling_outputs(
            output_path=output_path,
            allocations=city_scale_allocations,
            workload_file=workload_file,
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
            energy_cache={},
            wind_resource_cache={},
            energy_cache_locks={},
            wind_resource_cache_locks={},
            cache_locks_guard=cache_locks_guard,
            energy_calculator=energy_calculator,
            wind_calculator=wind_calculator,
            workers=_normalize_workers(workers),
            write_debug_scale_results=write_debug_scale_results,
        )
    )
    return output_files


def run_country_growth_load_shift_optimization(
    *,
    manifest_file: str | Path = CITY_MAP_FILE,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    include_not_ready: bool = False,
    cooling: str = "seawater",
    objectives: Iterable[str] = ("min-grid-mwh", "min-grid-co2"),
    workload_file: str | Path = WORKLOAD_FILE,
    idle_power_fraction: float = 0.35,
    hours: int | None = 8760,
    start_time: str | None = "2025-01-01 00:00",
    time_alignment: str | None = None,
    max_carbon_gap_hours: int = 6,
    battery_roundtrip_efficiency: float = 0.97,
    grid_import_limit_mw: float | None = None,
    load_shift_fraction: float = 0.3,
    hub_height_m: float = 150.0,
    wind_loss_fraction: float = 0.15,
    wind_cut_in: float = 3.0,
    wind_rated: float = 12.0,
    wind_cut_out: float = 25.0,
    workers: int = 1,
    countries: Iterable[str] | None = None,
    max_countries: int | None = None,
    country_rows: list[dict[str, object]] | None = None,
    city_rows: list[dict[str, object]] | None = None,
    scale_rows: list[dict[str, object]] | None = None,
    energy_calculator: EnergyCalculator = calculate_data_center_energy,
    wind_calculator: WindCalculator = calculate_wind_resource,
    optimizer: Optimizer = optimization,
    write_debug_scale_results: bool = False,
) -> dict[str, Path]:
    """Write city/country summaries for wind capacity demand and load-shift optimization."""
    output_path, country_growths, city_scale_allocations = _prepare_country_growth_inputs(
        manifest_file=manifest_file,
        output_dir=output_dir,
        include_not_ready=include_not_ready,
        country_rows=country_rows,
        city_rows=city_rows,
        scale_rows=scale_rows,
        countries=countries,
        max_countries=max_countries,
    )
    output_files = _write_foundation_outputs(output_path, country_growths, city_scale_allocations)
    cache_locks_guard = threading.Lock()
    output_files.update(
        _run_country_growth_load_shift_outputs(
            output_path=output_path,
            allocations=city_scale_allocations,
            cooling=cooling,
            objectives=tuple(objectives),
            workload_file=workload_file,
            idle_power_fraction=idle_power_fraction,
            hours=hours,
            start_time=start_time,
            time_alignment=time_alignment,
            max_carbon_gap_hours=max_carbon_gap_hours,
            battery_roundtrip_efficiency=battery_roundtrip_efficiency,
            grid_import_limit_mw=grid_import_limit_mw,
            load_shift_fraction=load_shift_fraction,
            hub_height_m=hub_height_m,
            wind_loss_fraction=wind_loss_fraction,
            wind_cut_in=wind_cut_in,
            wind_rated=wind_rated,
            wind_cut_out=wind_cut_out,
            energy_cache={},
            wind_resource_cache={},
            required_wind_cache={},
            energy_cache_locks={},
            wind_resource_cache_locks={},
            required_wind_cache_locks={},
            cache_locks_guard=cache_locks_guard,
            energy_calculator=energy_calculator,
            wind_calculator=wind_calculator,
            optimizer=optimizer,
            workers=_normalize_workers(workers),
            write_debug_scale_results=write_debug_scale_results,
        )
    )
    return output_files


def _prepare_country_growth_inputs(
    *,
    manifest_file: str | Path,
    output_dir: str | Path,
    include_not_ready: bool,
    country_rows: list[dict[str, object]] | None,
    city_rows: list[dict[str, object]] | None,
    scale_rows: list[dict[str, object]] | None,
    countries: Iterable[str] | None,
    max_countries: int | None,
) -> tuple[Path, pd.DataFrame, pd.DataFrame]:
    manifest_path = _resolve_path(manifest_file, ROOT_DIR)
    output_path = _resolve_output_dir(output_dir, ROOT_DIR)
    if country_rows is None:
        country_rows = _read_xlsx_sheet_rows(manifest_path, COUNTRY_MANIFEST_SHEET)
    if city_rows is None:
        city_rows = _read_xlsx_sheet_rows(manifest_path, CITY_MANIFEST_SHEET)
    if scale_rows is None:
        scale_rows = _read_xlsx_sheet_rows(manifest_path, DATACENTER_SCALE_SHEET)
    country_growths = build_country_growths(country_rows)
    city_scale_allocations = build_city_scale_allocations(
        country_growths=country_growths,
        city_rows=city_rows,
        scale_definitions=load_scale_definitions(scale_rows),
        include_not_ready=include_not_ready,
    )
    country_growths, city_scale_allocations = _filter_country_growth_inputs(
        country_growths=country_growths,
        city_scale_allocations=city_scale_allocations,
        countries=countries,
        max_countries=max_countries,
    )
    return output_path, country_growths, city_scale_allocations


def _run_country_growth_cooling_outputs(
    *,
    output_path: Path,
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
    energy_cache_locks: dict[tuple[object, ...], threading.Lock],
    wind_resource_cache_locks: dict[tuple[object, ...], threading.Lock],
    cache_locks_guard: threading.Lock,
    energy_calculator: EnergyCalculator,
    wind_calculator: WindCalculator,
    workers: int,
    write_debug_scale_results: bool,
) -> dict[str, Path]:
    cooling_city_scale = run_cooling_comparisons(
        allocations=allocations,
        workload_file=workload_file,
        idle_power_fraction=idle_power_fraction,
        hours=hours,
        start_time=start_time,
        time_alignment=_resolve_baseline_alignment(start_time, time_alignment),
        max_carbon_gap_hours=max_carbon_gap_hours,
        hub_height_m=hub_height_m,
        wind_loss_fraction=wind_loss_fraction,
        wind_cut_in=wind_cut_in,
        wind_rated=wind_rated,
        wind_cut_out=wind_cut_out,
        energy_cache=energy_cache,
        wind_resource_cache=wind_resource_cache,
        energy_cache_locks=energy_cache_locks,
        wind_resource_cache_locks=wind_resource_cache_locks,
        cache_locks_guard=cache_locks_guard,
        energy_calculator=energy_calculator,
        wind_calculator=wind_calculator,
        workers=workers,
    )
    cooling_city_results = select_all_scale_results(
        append_scale_totals(cooling_city_scale, COOLING_METRICS, extra_group_columns=["cooling_type"])
    )
    cooling_country_results = build_country_average_results(
        cooling_city_results,
        metric_columns=COOLING_METRICS,
        extra_group_columns=["cooling_type", "scale"],
    )
    cooling_city_summary = build_cooling_comparison_results(cooling_city_results)
    cooling_country_summary = build_cooling_comparison_results(cooling_country_results)
    _print_cooling_issue_summary(cooling_city_scale, context="after cooling calculations")

    suffix = _hours_token(hours)
    files = {
        "cooling_city_summary_csv": output_path / f"country_growth_cooling_city_summary_{suffix}.csv",
        "cooling_country_summary_csv": output_path / f"country_growth_cooling_country_summary_{suffix}.csv",
    }
    cooling_city_summary.to_csv(files["cooling_city_summary_csv"], index=False, encoding="utf-8-sig")
    cooling_country_summary.to_csv(files["cooling_country_summary_csv"], index=False, encoding="utf-8-sig")
    if write_debug_scale_results:
        files["cooling_scale_debug_csv"] = output_path / f"country_growth_cooling_scale_debug_{suffix}.csv"
        cooling_city_scale.to_csv(files["cooling_scale_debug_csv"], index=False, encoding="utf-8-sig")
    return files


def _run_country_growth_load_shift_outputs(
    *,
    output_path: Path,
    allocations: pd.DataFrame,
    cooling: str,
    objectives: tuple[str, ...],
    workload_file: str | Path,
    idle_power_fraction: float,
    hours: int | None,
    start_time: str | None,
    time_alignment: str | None,
    max_carbon_gap_hours: int,
    battery_roundtrip_efficiency: float,
    grid_import_limit_mw: float | None,
    load_shift_fraction: float,
    hub_height_m: float,
    wind_loss_fraction: float,
    wind_cut_in: float,
    wind_rated: float,
    wind_cut_out: float,
    energy_cache: dict[tuple[object, ...], DataCenterEnergyResult],
    wind_resource_cache: dict[tuple[object, ...], WindResourceResult],
    required_wind_cache: dict[tuple[object, ...], RequiredWindCapacity],
    energy_cache_locks: dict[tuple[object, ...], threading.Lock],
    wind_resource_cache_locks: dict[tuple[object, ...], threading.Lock],
    required_wind_cache_locks: dict[tuple[object, ...], threading.Lock],
    cache_locks_guard: threading.Lock,
    energy_calculator: EnergyCalculator,
    wind_calculator: WindCalculator,
    optimizer: Optimizer,
    workers: int,
    write_debug_scale_results: bool,
) -> dict[str, Path]:
    optimization_city_scale = run_optimization_comparisons(
        allocations=allocations,
        cooling=cooling,
        objectives=objectives,
        scenario_configs=_load_shift_scenario_configs(
            cooling=cooling,
            load_shift_fraction=load_shift_fraction,
        ),
        workload_file=workload_file,
        idle_power_fraction=idle_power_fraction,
        hours=hours,
        start_time=start_time,
        time_alignment=time_alignment,
        max_carbon_gap_hours=max_carbon_gap_hours,
        battery_capacity_mwh=0.0,
        battery_roundtrip_efficiency=battery_roundtrip_efficiency,
        grid_import_limit_mw=grid_import_limit_mw,
        battery_charge_limit_mw=0.0,
        battery_discharge_limit_mw=0.0,
        load_shift_fraction=load_shift_fraction,
        hub_height_m=hub_height_m,
        wind_loss_fraction=wind_loss_fraction,
        wind_cut_in=wind_cut_in,
        wind_rated=wind_rated,
        wind_cut_out=wind_cut_out,
        energy_cache=energy_cache,
        wind_resource_cache=wind_resource_cache,
        required_wind_cache=required_wind_cache,
        energy_cache_locks=energy_cache_locks,
        wind_resource_cache_locks=wind_resource_cache_locks,
        required_wind_cache_locks=required_wind_cache_locks,
        cache_locks_guard=cache_locks_guard,
        energy_calculator=energy_calculator,
        wind_calculator=wind_calculator,
        optimizer=optimizer,
        workers=workers,
    )
    optimization_city_results = select_all_scale_results(
        append_scale_totals(
            optimization_city_scale,
            OPTIMIZATION_RESULT_METRICS,
            extra_group_columns=["objective", "optimization_scenario", "optimization_scenario_label", "cooling_type"],
        )
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
    optimization_city_summary = build_optimization_comparison_results(optimization_city_results)
    optimization_country_summary = build_optimization_comparison_results(optimization_country_results)

    suffix = _hours_token(hours)
    files = {
        "load_shift_city_summary_csv": output_path / f"country_growth_load_shift_city_summary_{suffix}.csv",
        "load_shift_country_summary_csv": output_path / f"country_growth_load_shift_country_summary_{suffix}.csv",
    }
    optimization_city_summary.to_csv(files["load_shift_city_summary_csv"], index=False, encoding="utf-8-sig")
    optimization_country_summary.to_csv(files["load_shift_country_summary_csv"], index=False, encoding="utf-8-sig")
    if write_debug_scale_results:
        files["load_shift_scale_debug_csv"] = output_path / f"country_growth_load_shift_scale_debug_{suffix}.csv"
        optimization_city_scale.to_csv(files["load_shift_scale_debug_csv"], index=False, encoding="utf-8-sig")
    return files


def build_country_growths(country_rows: list[dict[str, object]]) -> pd.DataFrame:
    """Return country/scenario growth rows in MW."""
    if not country_rows:
        raise ValueError(f"Workbook sheet {COUNTRY_MANIFEST_SHEET} is empty.")
    columns = list(country_rows[0].keys())
    country_column = _find_column(columns, ["country", "country_area", "nation"], "country")
    coastal_share_column = _find_column(
        columns,
        ["coastal_share_of_total_pct", "coastal_share_pct", "coastal_pct"],
        "coastal share of total percent",
    )
    baseline_column = _find_2025_capacity_column(columns)
    scenario_columns = _find_2030_capacity_columns(columns)

    rows: list[dict[str, object]] = []
    for source_row in country_rows:
        country = _text(source_row.get(country_column))
        if not country:
            continue
        coastal_share_pct = _number(source_row.get(coastal_share_column), coastal_share_column)
        if coastal_share_pct < 0.0 or coastal_share_pct > 100.0:
            raise ValueError(
                f"{coastal_share_column} must be between 0 and 100 for country={country}; "
                f"got {coastal_share_pct:.6g}."
            )
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
                    "coastal_share_of_total_pct": coastal_share_pct,
                    "coastal_growth_mw": growth_mw * coastal_share_pct / 100.0,
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
    """Assign each country's coastal growth to every representative city and scale."""
    cities_by_country = _cities_by_country(city_rows, include_not_ready=include_not_ready)
    rows: list[dict[str, object]] = []
    for growth_row in country_growths.to_dict(orient="records"):
        country = _text(growth_row["country"])
        cities = cities_by_country.get(country, [])
        if not cities:
            raise ValueError(f"No representative cities found for country {country!r}.")
        country_growth_mw = float(growth_row["growth_mw"])
        city_growth_mw = float(growth_row["coastal_growth_mw"])
        for city in cities:
            for scale_definition in scale_definitions:
                scale_capacity_mw = city_growth_mw * scale_definition.ratio
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
                        "country_growth_mw": country_growth_mw,
                        "city_growth_mw": city_growth_mw,
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
    energy_cache_locks: dict[tuple[object, ...], threading.Lock] | None = None,
    wind_resource_cache_locks: dict[tuple[object, ...], threading.Lock] | None = None,
    cache_locks_guard: threading.Lock | None = None,
    energy_calculator: EnergyCalculator,
    wind_calculator: WindCalculator,
    workers: int = 1,
) -> pd.DataFrame:
    worker_count = _normalize_workers(workers)
    cache_guard = cache_locks_guard or threading.Lock()
    energy_locks = energy_cache_locks if energy_cache_locks is not None else {}
    wind_locks = wind_resource_cache_locks if wind_resource_cache_locks is not None else {}
    tasks: list[tuple[int, int, int, dict[str, object], str]] = []
    allocation_rows = allocations.to_dict(orient="records")
    total_allocations = len(allocation_rows)
    for allocation_index, allocation in enumerate(allocation_rows, start=1):
        for cooling_type in COOLING_TYPES:
            tasks.append((len(tasks), allocation_index, total_allocations, allocation, cooling_type))

    if _can_use_process_pool_for_cooling(
        workers=worker_count,
        energy_calculator=energy_calculator,
        wind_calculator=wind_calculator,
    ):
        context = CoolingTaskContext(
            total_tasks=len(tasks),
            workload_file=workload_file,
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
            energy_calculator=energy_calculator,
            wind_calculator=wind_calculator,
        )
        return pd.DataFrame(
            _run_parallel_task_groups(
                tasks=tasks,
                group_key=_task_country,
                run_group=_run_cooling_task_group,
                context=context,
                workers=worker_count,
            )
        )

    def run_task(task: tuple[int, int, int, dict[str, object], str]) -> tuple[int, dict[str, object]]:
        sequence, allocation_index, total_allocations, allocation, cooling_type = task
        print(
            "Cooling "
            f"{sequence + 1}/{len(tasks)} "
            f"(allocation {allocation_index}/{total_allocations}): "
            f"{allocation['country']} / {allocation['growth_scenario']} / "
            f"{allocation['city']} / {allocation['scale']} / {cooling_type}",
            flush=True,
        )
        row = _base_result_row(allocation)
        row["cooling_type"] = cooling_type
        try:
            if int(allocation["facility_count"]) == 0:
                return sequence, _zero_cooling_row(row, hours)
            rated_it_power_kw = float(allocation["facility_capacity_mw"]) * 1000.0
            energy = _get_energy_result(
                cache=energy_cache,
                cache_locks=energy_locks,
                cache_locks_guard=cache_guard,
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
                cache_locks=wind_locks,
                cache_locks_guard=cache_guard,
                wind_calculator=wind_calculator,
                city=str(allocation["city"]),
                hub_height_m=hub_height_m,
                loss_fraction=wind_loss_fraction,
                cut_in=wind_cut_in,
                rated=wind_rated,
                cut_out=wind_cut_out,
            )
            result_row = _cooling_result_row(
                row=row,
                energy=energy,
                wind_resource=wind_resource,
                facility_count=int(allocation["facility_count"]),
            )
            _print_cooling_row_issues(result_row)
            return sequence, result_row
        except Exception as exc:
            failed_row = _failed_row(row, str(exc))
            _print_cooling_row_issues(failed_row)
            return sequence, failed_row

    return pd.DataFrame(_run_parallel_tasks(tasks, run_task, worker_count, group_key=_task_country))


def run_optimization_comparisons(
    *,
    allocations: pd.DataFrame,
    cooling: str,
    objectives: tuple[str, ...],
    scenario_configs: tuple[dict[str, object], ...] | None = None,
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
    energy_cache_locks: dict[tuple[object, ...], threading.Lock] | None = None,
    wind_resource_cache_locks: dict[tuple[object, ...], threading.Lock] | None = None,
    required_wind_cache_locks: dict[tuple[object, ...], threading.Lock] | None = None,
    cache_locks_guard: threading.Lock | None = None,
    energy_calculator: EnergyCalculator,
    wind_calculator: WindCalculator,
    optimizer: Optimizer,
    workers: int = 1,
) -> pd.DataFrame:
    worker_count = _normalize_workers(workers)
    cache_guard = cache_locks_guard or threading.Lock()
    energy_locks = energy_cache_locks if energy_cache_locks is not None else {}
    wind_locks = wind_resource_cache_locks if wind_resource_cache_locks is not None else {}
    required_locks = required_wind_cache_locks if required_wind_cache_locks is not None else {}
    if scenario_configs is None:
        scenario_configs = _scenario_configs(
            cooling=cooling,
            load_shift_fraction=load_shift_fraction,
            battery_capacity_mwh=battery_capacity_mwh,
            battery_charge_limit_mw=battery_charge_limit_mw,
            battery_discharge_limit_mw=battery_discharge_limit_mw,
        )
    tasks: list[tuple[int, int, int, dict[str, object], dict[str, object], str]] = []
    allocation_rows = allocations.to_dict(orient="records")
    total_allocations = len(allocation_rows)
    for allocation_index, allocation in enumerate(allocation_rows, start=1):
        for scenario_config in scenario_configs:
            for objective in objectives:
                tasks.append(
                    (
                        len(tasks),
                        allocation_index,
                        total_allocations,
                        allocation,
                        scenario_config,
                        objective,
                    )
                )

    if _can_use_process_pool_for_optimization(
        workers=worker_count,
        energy_calculator=energy_calculator,
        wind_calculator=wind_calculator,
        optimizer=optimizer,
    ):
        context = OptimizationTaskContext(
            total_tasks=len(tasks),
            cooling=cooling,
            objectives=objectives,
            workload_file=workload_file,
            idle_power_fraction=idle_power_fraction,
            hours=hours,
            start_time=start_time,
            time_alignment=time_alignment,
            max_carbon_gap_hours=max_carbon_gap_hours,
            battery_roundtrip_efficiency=battery_roundtrip_efficiency,
            grid_import_limit_mw=grid_import_limit_mw,
            hub_height_m=hub_height_m,
            wind_loss_fraction=wind_loss_fraction,
            wind_cut_in=wind_cut_in,
            wind_rated=wind_rated,
            wind_cut_out=wind_cut_out,
            energy_calculator=energy_calculator,
            wind_calculator=wind_calculator,
            optimizer=optimizer,
        )
        return pd.DataFrame(
            _run_parallel_task_groups(
                tasks=tasks,
                group_key=_task_country,
                run_group=_run_optimization_task_group,
                context=context,
                workers=worker_count,
            )
        )

    def run_task(
        task: tuple[int, int, int, dict[str, object], dict[str, object], str],
    ) -> tuple[int, dict[str, object]]:
        sequence, allocation_index, total_allocations, allocation, scenario_config, objective = task
        optimization_scenario = str(scenario_config["scenario"])
        scenario_cooling = str(scenario_config["cooling_type"])
        print(
            "Optimization "
            f"{sequence + 1}/{len(tasks)} "
            f"(allocation {allocation_index}/{total_allocations}): "
            f"{allocation['country']} / {allocation['growth_scenario']} / "
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
                return sequence, _zero_optimization_row(row, hours)
            rated_it_power_kw = float(allocation["facility_capacity_mw"]) * 1000.0
            wind_capacity = _get_required_wind_capacity(
                cache=required_wind_cache,
                cache_locks=required_locks,
                cache_locks_guard=cache_guard,
                energy_cache=energy_cache,
                wind_resource_cache=wind_resource_cache,
                energy_cache_locks=energy_locks,
                wind_resource_cache_locks=wind_locks,
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
            return sequence, _optimization_result_row(
                row=row,
                wind_capacity=wind_capacity,
                result=result,
                facility_count=int(allocation["facility_count"]),
            )
        except Exception as exc:
            return sequence, _failed_row(row, str(exc))

    return pd.DataFrame(_run_parallel_tasks(tasks, run_task, worker_count, group_key=_task_country))


def _load_shift_scenario_configs(
    *,
    cooling: str,
    load_shift_fraction: float,
) -> tuple[dict[str, object], ...]:
    return (
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
    )


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


def select_all_scale_results(results: pd.DataFrame) -> pd.DataFrame:
    """Return only city or country rows representing the combined all-scale result."""
    if results.empty or "scale" not in results:
        return results.copy().reset_index(drop=True)
    return results[results["scale"] == "all_scales"].copy().reset_index(drop=True)


def build_cooling_comparison_results(results: pd.DataFrame) -> pd.DataFrame:
    """Compare seawater cooling against the air-source baseline."""
    if results.empty:
        return pd.DataFrame()
    group_columns = [
        column
        for column in ["country", "growth_scenario", "city", "scale"]
        if column in results.columns
    ]
    return _build_pairwise_comparison_results(
        results=results,
        group_columns=group_columns,
        compare_column="cooling_type",
        baseline_value="air_source",
        candidate_values=("seawater",),
        metric_columns=COOLING_METRICS,
        baseline_prefix="air_source",
        candidate_prefix="seawater",
        savings_suffix="vs_air_source",
    )


def build_optimization_comparison_results(results: pd.DataFrame) -> pd.DataFrame:
    """Compare optimization scenarios against the seawater baseline scenario."""
    if results.empty:
        return pd.DataFrame()
    group_columns = [
        column
        for column in ["country", "growth_scenario", "city", "objective", "cooling_type", "scale"]
        if column in results.columns
    ]
    candidate_values = [
        str(value)
        for value in results["optimization_scenario"].dropna().unique()
        if str(value) not in {"baseline", "baseline_air_source"}
    ]
    return _build_pairwise_comparison_results(
        results=results,
        group_columns=group_columns,
        compare_column="optimization_scenario",
        baseline_value="baseline",
        candidate_values=tuple(sorted(candidate_values)),
        metric_columns=OPTIMIZATION_RESULT_METRICS,
        baseline_prefix="baseline",
        candidate_prefix_column="optimization_scenario",
        savings_suffix="vs_baseline",
        label_column="optimization_scenario_label",
    )


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


def build_cooling_issue_summary(cooling_city_scale: pd.DataFrame) -> pd.DataFrame:
    """Return one row per cooling problem that can affect result validity."""
    if cooling_city_scale.empty:
        return pd.DataFrame(columns=COOLING_ISSUE_COLUMNS)
    rows: list[dict[str, object]] = []
    for row in cooling_city_scale.to_dict(orient="records"):
        rows.extend(_cooling_issue_rows(row))
    return pd.DataFrame(rows, columns=COOLING_ISSUE_COLUMNS)


def _print_cooling_row_issues(row: dict[str, object]) -> None:
    for issue in _cooling_issue_rows(row):
        location = " / ".join(
            str(issue.get(column, ""))
            for column in ["country", "growth_scenario", "city", "scale", "cooling_type"]
            if str(issue.get(column, "")).strip()
        )
        error = str(issue.get("error_message", "") or "").strip()
        detail = f"; error={error}" if error else ""
        print(
            f"{str(issue['severity']).upper()} {issue['issue_type']}: {location}: "
            f"{issue['message']}{detail}",
            flush=True,
        )


def _print_cooling_issue_summary(cooling_city_scale: pd.DataFrame, *, context: str) -> None:
    issues = build_cooling_issue_summary(cooling_city_scale)
    if issues.empty:
        print(f"Cooling issue summary {context}: 0 issue(s).", flush=True)
        return
    severity_counts = ", ".join(
        f"{severity}={count}" for severity, count in issues["severity"].value_counts().sort_index().items()
    )
    type_counts = ", ".join(
        f"{issue_type}={count}" for issue_type, count in issues["issue_type"].value_counts().sort_index().items()
    )
    print(
        f"Cooling issue summary {context}: {len(issues)} issue(s) ({severity_counts}; {type_counts}).",
        flush=True,
    )


def _cooling_issue_rows(row: dict[str, object]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    status = _issue_text(row.get("status")).lower()
    error_message = _issue_text(row.get("error_message"))
    if status and status != "ok":
        issues.append(
            _cooling_issue_row(
                row,
                severity="error",
                issue_type="task_failed",
                message="Cooling task failed; no valid result was produced for this row.",
                error_message=error_message,
            )
        )

    warning_count = _issue_float(row.get("model_warning_count"))
    if warning_count and warning_count > 0:
        warning_message = _issue_text(row.get("model_warning_messages")) or "Cooling model emitted warnings."
        issue_type = (
            "outlet_temperature_warning"
            if "outlet temperature" in warning_message.lower()
            else "model_warning"
        )
        issues.append(
            _cooling_issue_row(
                row,
                severity="warning",
                issue_type=issue_type,
                metric_value=warning_count,
                metric_unit="warning(s)",
                message=warning_message,
            )
        )

    unmet_kwh = _issue_float(row.get("unmet_cooling_energy_kwh"))
    if unmet_kwh and unmet_kwh > 1e-9:
        issues.append(
            _cooling_issue_row(
                row,
                severity="error",
                issue_type="unmet_cooling_load",
                metric_value=unmet_kwh,
                metric_unit="kWh",
                message="Cooling model reported unmet cooling load; energy and carbon results may be understated.",
            )
        )

    specific_constraint_reported = False
    seawater_temp_hours = _issue_float(row.get("seawater_temperature_violation_hours"))
    if seawater_temp_hours and seawater_temp_hours > 0:
        specific_constraint_reported = True
        issues.append(
            _cooling_issue_row(
                row,
                severity="warning",
                issue_type="seawater_temperature_violation",
                affected_hours=seawater_temp_hours,
                metric_value=seawater_temp_hours,
                metric_unit="hour(s)",
                message="Seawater source temperature exceeded the configured valid range.",
            )
        )

    outfall_hours = _issue_float(row.get("outfall_temperature_violation_hours"))
    if outfall_hours and outfall_hours > 0:
        specific_constraint_reported = True
        issues.append(
            _cooling_issue_row(
                row,
                severity="warning",
                issue_type="outfall_temperature_violation",
                affected_hours=outfall_hours,
                metric_value=outfall_hours,
                metric_unit="hour(s)",
                message="Seawater outfall temperature rise exceeded the configured limit.",
            )
        )

    constraint_hours = _issue_float(row.get("constraint_violation_hours"))
    if constraint_hours and constraint_hours > 0 and not specific_constraint_reported:
        issues.append(
            _cooling_issue_row(
                row,
                severity="warning",
                issue_type="cooling_constraint_violation",
                affected_hours=constraint_hours,
                metric_value=constraint_hours,
                metric_unit="hour(s)",
                message="Cooling model reported constraint violations; detailed violation type is unavailable.",
            )
        )
    return issues


def _cooling_issue_row(
    source: dict[str, object],
    *,
    severity: str,
    issue_type: str,
    message: str,
    affected_hours: float | None = None,
    metric_value: float | None = None,
    metric_unit: str = "",
    error_message: str = "",
) -> dict[str, object]:
    return {
        "severity": severity,
        "issue_type": issue_type,
        "country": source.get("country", ""),
        "growth_scenario": source.get("growth_scenario", ""),
        "city": source.get("city", ""),
        "scale": source.get("scale", ""),
        "cooling_type": source.get("cooling_type", ""),
        "hours": source.get("hours", ""),
        "affected_hours": "" if affected_hours is None else affected_hours,
        "metric_value": "" if metric_value is None else metric_value,
        "metric_unit": metric_unit,
        "facility_count": source.get("facility_count", ""),
        "facility_capacity_mw": source.get("facility_capacity_mw", ""),
        "message": message,
        "error_message": error_message,
    }


def _issue_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _issue_text(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _run_cooling_task_group(
    group_tasks: list[object],
    context: CoolingTaskContext,
) -> list[tuple[int, dict[str, object]]]:
    energy_cache: dict[tuple[object, ...], DataCenterEnergyResult] = {}
    wind_resource_cache: dict[tuple[object, ...], WindResourceResult] = {}
    return [
        _run_cooling_task(
            task,
            context,
            energy_cache=energy_cache,
            wind_resource_cache=wind_resource_cache,
        )
        for task in group_tasks
    ]


def _run_cooling_task(
    task: object,
    context: CoolingTaskContext,
    *,
    energy_cache: dict[tuple[object, ...], DataCenterEnergyResult],
    wind_resource_cache: dict[tuple[object, ...], WindResourceResult],
) -> tuple[int, dict[str, object]]:
    sequence, allocation_index, total_allocations, allocation, cooling_type = task  # type: ignore[misc]
    print(
        "Cooling "
        f"{sequence + 1}/{context.total_tasks} "
        f"(allocation {allocation_index}/{total_allocations}): "
        f"{allocation['country']} / {allocation['growth_scenario']} / "
        f"{allocation['city']} / {allocation['scale']} / {cooling_type}",
        flush=True,
    )
    row = _base_result_row(allocation)
    row["cooling_type"] = cooling_type
    try:
        if int(allocation["facility_count"]) == 0:
            return sequence, _zero_cooling_row(row, context.hours)
        rated_it_power_kw = float(allocation["facility_capacity_mw"]) * 1000.0
        energy = _get_energy_result(
            cache=energy_cache,
            cache_locks=None,
            cache_locks_guard=None,
            energy_calculator=context.energy_calculator,
            city=str(allocation["city"]),
            cooling_type=str(cooling_type),
            workload_file=context.workload_file,
            rated_it_power_kw=rated_it_power_kw,
            idle_power_fraction=context.idle_power_fraction,
            hours=context.hours,
            start_time=context.start_time,
            time_alignment=context.time_alignment,
            max_carbon_gap_hours=context.max_carbon_gap_hours,
        )
        wind_resource = _get_wind_resource(
            cache=wind_resource_cache,
            cache_locks=None,
            cache_locks_guard=None,
            wind_calculator=context.wind_calculator,
            city=str(allocation["city"]),
            hub_height_m=context.hub_height_m,
            loss_fraction=context.wind_loss_fraction,
            cut_in=context.wind_cut_in,
            rated=context.wind_rated,
            cut_out=context.wind_cut_out,
            )
        result_row = _cooling_result_row(
            row=row,
            energy=energy,
            wind_resource=wind_resource,
            facility_count=int(allocation["facility_count"]),
        )
        _print_cooling_row_issues(result_row)
        return sequence, result_row
    except Exception as exc:
        failed_row = _failed_row(row, str(exc))
        _print_cooling_row_issues(failed_row)
        return sequence, failed_row


def _run_optimization_task_group(
    group_tasks: list[object],
    context: OptimizationTaskContext,
) -> list[tuple[int, dict[str, object]]]:
    energy_cache: dict[tuple[object, ...], DataCenterEnergyResult] = {}
    wind_resource_cache: dict[tuple[object, ...], WindResourceResult] = {}
    required_wind_cache: dict[tuple[object, ...], RequiredWindCapacity] = {}
    return [
        _run_optimization_task(
            task,
            context,
            energy_cache=energy_cache,
            wind_resource_cache=wind_resource_cache,
            required_wind_cache=required_wind_cache,
        )
        for task in group_tasks
    ]


def _run_optimization_task(
    task: object,
    context: OptimizationTaskContext,
    *,
    energy_cache: dict[tuple[object, ...], DataCenterEnergyResult],
    wind_resource_cache: dict[tuple[object, ...], WindResourceResult],
    required_wind_cache: dict[tuple[object, ...], RequiredWindCapacity],
) -> tuple[int, dict[str, object]]:
    sequence, allocation_index, total_allocations, allocation, scenario_config, objective = task  # type: ignore[misc]
    optimization_scenario = str(scenario_config["scenario"])
    scenario_cooling = str(scenario_config["cooling_type"])
    print(
        "Optimization "
        f"{sequence + 1}/{context.total_tasks} "
        f"(allocation {allocation_index}/{total_allocations}): "
        f"{allocation['country']} / {allocation['growth_scenario']} / "
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
            return sequence, _zero_optimization_row(row, context.hours)
        rated_it_power_kw = float(allocation["facility_capacity_mw"]) * 1000.0
        wind_capacity = _get_required_wind_capacity(
            cache=required_wind_cache,
            cache_locks=None,
            cache_locks_guard=None,
            energy_cache=energy_cache,
            wind_resource_cache=wind_resource_cache,
            energy_cache_locks=None,
            wind_resource_cache_locks=None,
            energy_calculator=context.energy_calculator,
            wind_calculator=context.wind_calculator,
            city=str(allocation["city"]),
            cooling_type=scenario_cooling,
            workload_file=context.workload_file,
            rated_it_power_kw=rated_it_power_kw,
            idle_power_fraction=context.idle_power_fraction,
            hours=context.hours,
            start_time=context.start_time,
            time_alignment=context.time_alignment,
            max_carbon_gap_hours=context.max_carbon_gap_hours,
            hub_height_m=context.hub_height_m,
            wind_loss_fraction=context.wind_loss_fraction,
            wind_cut_in=context.wind_cut_in,
            wind_rated=context.wind_rated,
            wind_cut_out=context.wind_cut_out,
        )
        result = context.optimizer(
            city=str(allocation["city"]),
            cooling=scenario_cooling,
            wind_capacity_mw=wind_capacity.required_wind_capacity_mw,
            wind_nc_file=wind_capacity.wind_nc_file,
            workload_file=context.workload_file,
            rated_it_power_kw=rated_it_power_kw,
            battery_capacity_mwh=scenario_config["battery_capacity_mwh"],
            battery_roundtrip_efficiency=context.battery_roundtrip_efficiency,
            grid_import_limit_mw=context.grid_import_limit_mw,
            battery_charge_limit_mw=scenario_config["battery_charge_limit_mw"],
            battery_discharge_limit_mw=scenario_config["battery_discharge_limit_mw"],
            load_shift_fraction=scenario_config["load_shift_fraction"],
            hours=context.hours,
            start_time=context.start_time,
            time_alignment=context.time_alignment,
            max_carbon_gap_hours=context.max_carbon_gap_hours,
            hub_height_m=context.hub_height_m,
            wind_loss_fraction=context.wind_loss_fraction,
            wind_cut_in=context.wind_cut_in,
            wind_rated=context.wind_rated,
            wind_cut_out=context.wind_cut_out,
            objective=objective,
            include_hourly=True,
            output_results=False,
        )
        return sequence, _optimization_result_row(
            row=row,
            wind_capacity=wind_capacity,
            result=result,
            facility_count=int(allocation["facility_count"]),
        )
    except Exception as exc:
        return sequence, _failed_row(row, str(exc))


def _run_parallel_task_groups(
    *,
    tasks: list[object],
    group_key: Callable[[object], object],
    run_group: Callable[[list[object], object], list[tuple[int, dict[str, object]]]],
    context: object,
    workers: int,
) -> list[dict[str, object]]:
    grouped_tasks = _group_tasks(tasks, group_key)
    if not grouped_tasks:
        return []
    group_items = list(grouped_tasks.items())
    completed: list[tuple[int, dict[str, object]]] = []
    if workers <= 1:
        for _, group in group_items:
            completed.extend(run_group(group, context))
    else:
        try:
            with ProcessPoolExecutor(max_workers=min(workers, len(group_items))) as executor:
                futures = [executor.submit(run_group, group, context) for _, group in group_items]
                for future in as_completed(futures):
                    completed.extend(future.result())
        except BrokenProcessPool as exc:
            print(
                "Process pool worker terminated abruptly; retrying grouped tasks with "
                f"ThreadPoolExecutor ({min(workers, len(group_items))} workers). "
                f"Original error: {exc}",
                file=sys.stderr,
                flush=True,
            )
            completed = _run_task_groups_with_threads(
                group_items=group_items,
                run_group=run_group,
                context=context,
                workers=workers,
            )
    return [row for _, row in sorted(completed, key=lambda item: item[0])]


def _run_task_groups_with_threads(
    *,
    group_items: list[tuple[object, list[object]]],
    run_group: Callable[[list[object], object], list[tuple[int, dict[str, object]]]],
    context: object,
    workers: int,
) -> list[tuple[int, dict[str, object]]]:
    if not group_items:
        return []
    worker_count = min(_normalize_workers(workers), len(group_items))
    if worker_count <= 1:
        completed: list[tuple[int, dict[str, object]]] = []
        for _, group in group_items:
            completed.extend(run_group(group, context))
        return completed

    completed = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(run_group, group, context) for _, group in group_items]
        for future in as_completed(futures):
            completed.extend(future.result())
    return completed


def _run_parallel_tasks(
    tasks: list[object],
    run_task: Callable[[object], tuple[int, dict[str, object]]],
    workers: int,
    group_key: Callable[[object], object] | None = None,
) -> list[dict[str, object]]:
    if not tasks:
        return []
    if workers <= 1:
        completed = [run_task(task) for task in tasks]
    elif group_key is not None:
        completed = []
        grouped_tasks = _group_tasks(tasks, group_key)

        def run_group(group_tasks: list[object]) -> list[tuple[int, dict[str, object]]]:
            return [run_task(task) for task in group_tasks]

        with ThreadPoolExecutor(max_workers=min(workers, len(grouped_tasks))) as executor:
            futures = [executor.submit(run_group, group) for group in grouped_tasks.values()]
            for future in as_completed(futures):
                completed.extend(future.result())
    else:
        completed = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(run_task, task) for task in tasks]
            for future in as_completed(futures):
                completed.append(future.result())
    return [row for _, row in sorted(completed, key=lambda item: item[0])]


def _group_tasks(
    tasks: list[object],
    group_key: Callable[[object], object],
) -> dict[object, list[object]]:
    grouped_tasks: dict[object, list[object]] = {}
    for task in tasks:
        grouped_tasks.setdefault(group_key(task), []).append(task)
    return grouped_tasks


def _can_use_process_pool_for_cooling(
    *,
    workers: int,
    energy_calculator: EnergyCalculator,
    wind_calculator: WindCalculator,
) -> bool:
    return (
        workers > 1
        and energy_calculator is calculate_data_center_energy
        and wind_calculator is calculate_wind_resource
    )


def _can_use_process_pool_for_optimization(
    *,
    workers: int,
    energy_calculator: EnergyCalculator,
    wind_calculator: WindCalculator,
    optimizer: Optimizer,
) -> bool:
    return (
        workers > 1
        and energy_calculator is calculate_data_center_energy
        and wind_calculator is calculate_wind_resource
        and optimizer is optimization
    )


def _filter_country_growth_inputs(
    *,
    country_growths: pd.DataFrame,
    city_scale_allocations: pd.DataFrame,
    countries: Iterable[str] | None,
    max_countries: int | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    selected_countries = list(dict.fromkeys(city_scale_allocations["country"].astype(str)))
    if countries:
        requested = {str(country).strip() for country in countries if str(country).strip()}
        selected_countries = [country for country in selected_countries if country in requested]
        missing = sorted(requested.difference(selected_countries))
        if missing:
            raise ValueError(f"Countries not found in allocations: {', '.join(missing)}")
    if max_countries is not None:
        limit = int(max_countries)
        if limit < 1:
            raise ValueError("max_countries must be a positive integer.")
        selected_countries = selected_countries[:limit]
    if not selected_countries:
        raise ValueError("No countries selected for country growth allocation.")
    country_filter = country_growths["country"].astype(str).isin(selected_countries)
    allocation_filter = city_scale_allocations["country"].astype(str).isin(selected_countries)
    return (
        country_growths.loc[country_filter].reset_index(drop=True),
        city_scale_allocations.loc[allocation_filter].reset_index(drop=True),
    )


def _task_country(task: object) -> object:
    if isinstance(task, tuple) and len(task) >= 4 and isinstance(task[3], dict):
        return task[3].get("country", "")
    return ""


def _build_pairwise_comparison_results(
    *,
    results: pd.DataFrame,
    group_columns: list[str],
    compare_column: str,
    baseline_value: str,
    candidate_values: tuple[str, ...],
    metric_columns: list[str],
    baseline_prefix: str,
    savings_suffix: str,
    candidate_prefix: str | None = None,
    candidate_prefix_column: str | None = None,
    label_column: str | None = None,
) -> pd.DataFrame:
    if compare_column not in results.columns:
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    for _, group in results.groupby(group_columns, dropna=False, sort=True):
        baseline = _first_matching_row(group, compare_column, baseline_value)
        for candidate_value in candidate_values:
            candidate = _first_matching_row(group, compare_column, candidate_value)
            if baseline is None and candidate is None:
                continue
            source = candidate if candidate is not None else baseline
            assert source is not None
            row = _comparison_metadata(source, group_columns)
            metric_candidate_prefix = (
                "comparison"
                if candidate_prefix_column is not None
                else str(candidate_prefix or candidate_value)
            )
            row.update(
                {
                    "comparison": f"{candidate_value}_vs_{baseline_value}",
                    f"baseline_{compare_column}": baseline_value,
                    f"comparison_{compare_column}": candidate_value,
                    "baseline_status": _row_value(baseline, "status", "missing"),
                    "comparison_status": _row_value(candidate, "status", "missing"),
                    "status": _comparison_status(baseline, candidate),
                    "error_message": _comparison_error_message(baseline, candidate),
                }
            )
            if label_column:
                row[f"comparison_{label_column}"] = _row_value(candidate, label_column, candidate_value)
            for metadata_column in _COMPARISON_METADATA_COLUMNS:
                if metadata_column in source.index and metadata_column not in row:
                    row[metadata_column] = source[metadata_column]
            for metric in metric_columns:
                baseline_metric = _row_numeric_value(baseline, metric)
                candidate_metric = _row_numeric_value(candidate, metric)
                savings = baseline_metric - candidate_metric
                row[f"{baseline_prefix}_{metric}"] = baseline_metric
                row[f"{metric_candidate_prefix}_{metric}"] = candidate_metric
                row[f"{metric}_savings_{savings_suffix}"] = savings
                row[f"{metric}_savings_pct_{savings_suffix}"] = _pct(savings, baseline_metric)
            rows.append(row)
    return pd.DataFrame(rows)


_COMPARISON_METADATA_COLUMNS = [
    "representative_city_count",
    "city_count_in_country",
    "country_growth_mw",
    "city_growth_mw",
    "average_city_growth_mw",
    "scale",
    "scale_share",
    "scale_capacity_mw",
    "average_scale_capacity_mw",
    "facility_count",
    "average_facility_count",
    "below_scale_min",
    "below_scale_min_city_count",
]


def _first_matching_row(group: pd.DataFrame, column: str, value: str) -> pd.Series | None:
    matches = group[group[column].astype(str) == value]
    if matches.empty:
        return None
    return matches.iloc[0]


def _comparison_metadata(source: pd.Series, group_columns: list[str]) -> dict[str, object]:
    return {column: source[column] for column in group_columns if column in source.index}


def _comparison_status(baseline: pd.Series | None, candidate: pd.Series | None) -> str:
    if baseline is None or candidate is None:
        return "failed"
    statuses = {str(_row_value(baseline, "status", "")), str(_row_value(candidate, "status", ""))}
    return "ok" if statuses == {"ok"} else "failed"


def _comparison_error_message(baseline: pd.Series | None, candidate: pd.Series | None) -> str:
    errors: list[str] = []
    if baseline is None:
        errors.append("Missing baseline row")
    if candidate is None:
        errors.append("Missing comparison row")
    for label, row in (("baseline", baseline), ("comparison", candidate)):
        message = str(_row_value(row, "error_message", "") or "").strip()
        if message:
            errors.append(f"{label}: {message}")
    return "; ".join(errors)


def _get_energy_result(
    *,
    cache: dict[tuple[object, ...], DataCenterEnergyResult],
    cache_locks: dict[tuple[object, ...], threading.Lock] | None = None,
    cache_locks_guard: threading.Lock | None = None,
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
        lock = _cache_key_lock(cache_locks, cache_locks_guard, key)
        with lock:
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
    cache_locks: dict[tuple[object, ...], threading.Lock] | None = None,
    cache_locks_guard: threading.Lock | None = None,
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
        lock = _cache_key_lock(cache_locks, cache_locks_guard, key)
        with lock:
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
    cache_locks: dict[tuple[object, ...], threading.Lock] | None = None,
    cache_locks_guard: threading.Lock | None = None,
    energy_cache: dict[tuple[object, ...], DataCenterEnergyResult],
    wind_resource_cache: dict[tuple[object, ...], WindResourceResult],
    energy_cache_locks: dict[tuple[object, ...], threading.Lock] | None = None,
    wind_resource_cache_locks: dict[tuple[object, ...], threading.Lock] | None = None,
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
        lock = _cache_key_lock(cache_locks, cache_locks_guard, key)
        with lock:
            if key not in cache:
                energy = _get_energy_result(
                    cache=energy_cache,
                    cache_locks=energy_cache_locks,
                    cache_locks_guard=cache_locks_guard,
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
                    cache_locks=wind_resource_cache_locks,
                    cache_locks_guard=cache_locks_guard,
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


def _cache_key_lock(
    cache_locks: dict[tuple[object, ...], threading.Lock] | None,
    cache_locks_guard: threading.Lock | None,
    key: tuple[object, ...],
) -> threading.Lock:
    if cache_locks is None:
        return threading.Lock()
    guard = cache_locks_guard or threading.Lock()
    with guard:
        if key not in cache_locks:
            cache_locks[key] = threading.Lock()
        return cache_locks[key]


def _normalize_workers(workers: int) -> int:
    worker_count = int(workers)
    if worker_count <= 0:
        raise ValueError("workers must be a positive integer.")
    return worker_count


def _normalize_mode(mode: str) -> str:
    normalized = str(mode).strip().lower().replace("_", "-")
    aliases = {
        "all": "all",
        "cooling": "cooling",
        "heat-pump": "cooling",
        "load-shift": "load-shift",
        "loadshift": "load-shift",
        "optimization": "load-shift",
    }
    if normalized not in aliases:
        raise ValueError("mode must be one of: all, cooling, load-shift.")
    return aliases[normalized]


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
            "unmet_cooling_energy_kwh": float(getattr(energy, "unmet_cooling_energy_kwh", 0.0) or 0.0)
            * multiplier,
            "constraint_violation_hours": float(getattr(energy, "constraint_violation_hours", 0.0) or 0.0),
            "outfall_temperature_violation_hours": float(
                getattr(energy, "outfall_temperature_violation_hours", 0.0) or 0.0
            ),
            "seawater_temperature_violation_hours": float(
                getattr(energy, "seawater_temperature_violation_hours", 0.0) or 0.0
            ),
            "max_seawater_flow_rate_m3_s": float(
                getattr(energy, "max_seawater_flow_rate_m3_s", 0.0) or 0.0
            ),
            "max_seawater_heat_exchange_unit_count": float(
                getattr(energy, "max_seawater_heat_exchange_unit_count", 0.0) or 0.0
            ),
            "model_warning_count": int(getattr(energy, "model_warning_count", 0) or 0),
            "model_warning_messages": str(getattr(energy, "model_warning_messages", "") or ""),
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
    for column in COOLING_DIAGNOSTIC_COLUMNS:
        row[column] = "" if column == "model_warning_messages" else 0.0
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


def _default_worker_count() -> int:
    return 15


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Allocate country 2030 growth to representative cities and compare cooling/optimization scenarios."
    )
    parser.add_argument("--manifest-file", default=str(CITY_MAP_FILE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--include-not-ready", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--countries",
        nargs="+",
        default=None,
        help="Limit the run to one or more country names, for example --countries China Japan.",
    )
    parser.add_argument(
        "--max-countries",
        type=int,
        default=None,
        help="Limit the run to the first N selected countries for quick validation.",
    )
    parser.add_argument(
        "--mode",
        choices=["all", "cooling", "load-shift"],
        default="cooling",
        help="Run cooling comparison, load-shift optimization, or both.",
    )
    parser.add_argument(
        "--write-debug-scale-results",
        action="store_true",
        help="Write scale-level cooling and optimization debug CSVs in addition to all-scale paper outputs.",
    )
    parser.add_argument("--workload-file", default=str(WORKLOAD_FILE))
    parser.add_argument("--idle-power-fraction", type=float, default=0.3, help="0.1 0.23 0.35")
    parser.add_argument("--hours", type=int, default=8760)
    parser.add_argument("--start-time", default="2025-01-01 00:00")
    parser.add_argument("--time-alignment", choices=["sst", "latest", "start_time"], default=None)
    parser.add_argument("--max-carbon-gap-hours", type=int, default=6)
    parser.add_argument("--cooling", choices=["seawater", "air_source"], default="seawater")
    parser.add_argument("--objectives", nargs="+", default=["min-grid-co2"],help="min-grid-mwh",)
    parser.add_argument("--battery-capacity-mwh", type=float, default=535.4)
    parser.add_argument("--battery-roundtrip-efficiency", type=float, default=0.97)
    parser.add_argument("--grid-import-limit-mw", type=float, default=None)
    parser.add_argument("--battery-charge-limit-mw", type=float, default=25.0)
    parser.add_argument("--battery-discharge-limit-mw", type=float, default=25.0)
    parser.add_argument("--load-shift-fraction", type=float, default=0.3, help="0.15 0.3 0.45")
    parser.add_argument("--hub-height-m", type=float, default=150.0)
    parser.add_argument("--wind-loss-fraction", type=float, default=0.15, help="0.1 0.15 0.2")
    parser.add_argument("--wind-cut-in", type=float, default=3.0)
    parser.add_argument("--wind-rated", type=float, default=12.0)
    parser.add_argument("--wind-cut-out", type=float, default=25.0)
    parser.add_argument(
        "--workers",
        type=int,
        default=_default_worker_count(),
        help=(
            "Number of worker processes for cooling and optimization calculations. "
            "Default 15 runs countries in parallel and each country serially."
        ),
    )
    return parser


if __name__ == "__main__":
    args = _build_arg_parser().parse_args(sys.argv[1:])
    args.mode = 'load-shift'   # ["cooling", "load-shift"]
    args.workers = 2
    mode = _normalize_mode(args.mode)

    if args.dry_run:
        output_files = run_country_growth_allocation(
            manifest_file=args.manifest_file,
            output_dir=args.output_dir,
            include_not_ready=args.include_not_ready,
            dry_run=True,
            countries=args.countries,
            max_countries=args.max_countries,
            mode=mode,
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
            workers=args.workers,
            write_debug_scale_results=args.write_debug_scale_results,
        )
    else:
        output_files: dict[str, Path] = {}
        if mode in {"all", "cooling"}:
            output_files.update(
                run_country_growth_cooling_comparison(
                    manifest_file=args.manifest_file,
                    output_dir=args.output_dir,
                    include_not_ready=args.include_not_ready,
                    countries=args.countries,
                    max_countries=args.max_countries,
                    workload_file=args.workload_file,
                    idle_power_fraction=args.idle_power_fraction,
                    hours=args.hours,
                    start_time=args.start_time,
                    time_alignment=args.time_alignment,
                    max_carbon_gap_hours=args.max_carbon_gap_hours,
                    hub_height_m=args.hub_height_m,
                    wind_loss_fraction=args.wind_loss_fraction,
                    wind_cut_in=args.wind_cut_in,
                    wind_rated=args.wind_rated,
                    wind_cut_out=args.wind_cut_out,
                    workers=args.workers,
                    write_debug_scale_results=args.write_debug_scale_results,
                )
            )
        if mode in {"all", "load-shift"}:
            output_files.update(
                run_country_growth_load_shift_optimization(
                    manifest_file=args.manifest_file,
                    output_dir=args.output_dir,
                    include_not_ready=args.include_not_ready,
                    countries=args.countries,
                    max_countries=args.max_countries,
                    cooling=args.cooling,
                    objectives=tuple(args.objectives),
                    workload_file=args.workload_file,
                    idle_power_fraction=args.idle_power_fraction,
                    hours=args.hours,
                    start_time=args.start_time,
                    time_alignment=args.time_alignment,
                    max_carbon_gap_hours=args.max_carbon_gap_hours,
                    battery_roundtrip_efficiency=args.battery_roundtrip_efficiency,
                    grid_import_limit_mw=args.grid_import_limit_mw,
                    load_shift_fraction=args.load_shift_fraction,
                    hub_height_m=args.hub_height_m,
                    wind_loss_fraction=args.wind_loss_fraction,
                    wind_cut_in=args.wind_cut_in,
                    wind_rated=args.wind_rated,
                    wind_cut_out=args.wind_cut_out,
                    workers=args.workers,
                    write_debug_scale_results=args.write_debug_scale_results,
                )
            )
    print({key: str(path) for key, path in output_files.items()})
