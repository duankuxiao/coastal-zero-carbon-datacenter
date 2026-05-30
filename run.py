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
import json
import math
import re
import sys
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

from utils.tools import (_resolve_baseline_alignment, _resolve_path, _resolve_output_dir, _hours_token, _number,
                         _text, _is_ready, _normalize_column, _find_column, _capacity_to_mw,
                         _capacity_unit_from_column, _scenario_label_from_column)

ROOT_DIR = Path(__file__).resolve().parent
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

from utils.output_tables import (
    append_scale_totals,
    select_all_scale_results,
    write_cooling_output_tables,
    write_csv as _write_csv,
    write_optimization_output_tables,
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
DEFAULT_CONFIG_FILE = ROOT_DIR / "scripts" / "run_config.txt"
OPTIMIZATION_RESULT_METRICS = [
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
    "shifted_down_mwh",
    "shifted_up_mwh",
    "load_movement_budget_used_fraction",
    "hours_with_grid_purchase",
    "hours_with_curtailment",
    "max_hourly_grid_purchase_mw",
    "max_hourly_wind_curtailment_mw",
]
OPTIMIZATION_HOURLY_RESULT_KEYS = {
    "optimized_demand_mwh",
    "grid_purchase_hourly_mwh",
    "wind_curtailment_hourly_mwh",
}
OPTIMIZATION_SCENARIO_LABELS = {
    "baseline": "baseline",
    "load_shift": "load shift",
}


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
    server_energy_kwh: float
    server_carbon_emissions_kgco2: float
    cooling_energy_kwh: float
    cooling_carbon_emissions_kgco2: float
    total_energy_kwh: float
    total_carbon_emissions_kgco2: float
    datacenter_total_energy_mwh: float
    required_wind_capacity_mw: float
    wind_annual_generation_mwh: float
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
    sst_fraction: float
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
    sst_fraction: float
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


def run_configured_cases(
    *,
    config_file: str | Path = DEFAULT_CONFIG_FILE,
    manifest_file: str | Path | None = None,
    output_dir: str | Path | None = None,
    workload_file: str | Path | None = None,
    include_not_ready: bool | None = None,
    dry_run: bool = False,
    idle_power_fraction: float | None = None,
    hours: int | None = None,
    start_time: str | None = None,
    time_alignment: str | None = None,
    max_carbon_gap_hours: int | None = None,
    sst_fraction: float | None = None,
    grid_import_limit_mw: float | None = None,
    load_shift_fraction: float | None = None,
    optimization_objective: str | None = None,
    hub_height_m: float | None = None,
    wind_loss_fraction: float | None = None,
    wind_cut_in: float | None = None,
    wind_rated: float | None = None,
    wind_cut_out: float | None = None,
    workers: int | None = None,
    countries: Iterable[str] | None = None,
    max_countries: int | None = None,
    energy_calculator: EnergyCalculator = calculate_data_center_energy,
    wind_calculator: WindCalculator = calculate_wind_resource,
    optimizer: Optimizer = optimization,
    write_debug_scale_results: bool | None = None,
) -> dict[str, Path]:
    """Run all configured cases from a text config file."""
    config_path = _resolve_path(config_file, ROOT_DIR)
    config = _load_run_config(config_path)
    cases = _normalize_run_cases(config.get("cases"))
    if optimization_objective is not None:
        objective_value = _normalize_optimization_objective(optimization_objective)
        cases = tuple(
            {**case, "optimization_objectives": (objective_value,)}
            if bool(case["optimization_enabled"])
            else case
            for case in cases
        )

    manifest_value = manifest_file if manifest_file is not None else config.get("manifest_file", CITY_MAP_FILE)
    output_value = output_dir if output_dir is not None else config.get("output_dir", DEFAULT_OUTPUT_DIR)
    workload_value = workload_file if workload_file is not None else config.get("workload_file", WORKLOAD_FILE)
    include_ready_value = (
        bool(config.get("include_not_ready", False))
        if include_not_ready is None
        else bool(include_not_ready)
    )
    debug_value = (
        bool(config.get("write_debug_scale_results", False))
        if write_debug_scale_results is None
        else bool(write_debug_scale_results)
    )
    dry_run_value = bool(config.get("dry_run", False)) or bool(dry_run)

    idle_power_value = float(_config_override(config, "idle_power_fraction", idle_power_fraction, 0.23))
    hours_value = _optional_int(_config_override(config, "hours", hours, 8760))
    start_time_value = _optional_text(_config_override(config, "start_time", start_time, "2025-01-01 00:00"))
    time_alignment_value = _optional_text(_config_override(config, "time_alignment", time_alignment, None))
    max_carbon_gap_value = int(_config_override(config, "max_carbon_gap_hours", max_carbon_gap_hours, 6))
    sst_default = config.get("sst_fraction", config.get("sst_fraction", 1.0))
    sst_fraction_value = float(sst_fraction if sst_fraction is not None else sst_default)
    grid_import_value = _optional_float(
        _config_override(config, "grid_import_limit_mw", grid_import_limit_mw, None)
    )
    load_shift_fraction_value = float(_config_override(config, "load_shift_fraction", load_shift_fraction, 0.3))
    hub_height_value = float(_config_override(config, "hub_height_m", hub_height_m, 150.0))
    wind_loss_value = float(_config_override(config, "wind_loss_fraction", wind_loss_fraction, 0.15))
    wind_cut_in_value = float(_config_override(config, "wind_cut_in", wind_cut_in, 3.0))
    wind_rated_value = float(_config_override(config, "wind_rated", wind_rated, 12.0))
    wind_cut_out_value = float(_config_override(config, "wind_cut_out", wind_cut_out, 25.0))
    worker_value = _normalize_workers(int(_config_override(config, "workers", workers, _default_worker_count())))

    output_path, country_growths, city_scale_allocations = _prepare_country_growth_inputs(
        manifest_file=manifest_value,
        output_dir=output_value,
        include_not_ready=include_ready_value,
        country_rows=None,
        city_rows=None,
        scale_rows=None,
        countries=countries,
        max_countries=max_countries,
    )

    output_files: dict[str, Path] = {}
    if dry_run_value:
        output_files.update(_write_foundation_outputs(output_path, country_growths, city_scale_allocations))
        print(f"Dry run complete. Foundation CSVs written under {output_path}")
        return output_files

    energy_cache: dict[tuple[object, ...], DataCenterEnergyResult] = {}
    wind_resource_cache: dict[tuple[object, ...], WindResourceResult] = {}
    required_wind_cache: dict[tuple[object, ...], RequiredWindCapacity] = {}
    energy_cache_locks: dict[tuple[object, ...], threading.Lock] = {}
    wind_resource_cache_locks: dict[tuple[object, ...], threading.Lock] = {}
    required_wind_cache_locks: dict[tuple[object, ...], threading.Lock] = {}
    cache_locks_guard = threading.Lock()

    baseline_cooling_types = _baseline_cooling_types(cases)
    if baseline_cooling_types:
        output_files.update(
            _run_country_growth_cooling_outputs(
                output_path=output_path,
                allocations=city_scale_allocations,
                cooling_types=baseline_cooling_types,
                workload_file=workload_value,
                idle_power_fraction=idle_power_value,
                hours=hours_value,
                start_time=start_time_value,
                time_alignment=time_alignment_value,
                max_carbon_gap_hours=max_carbon_gap_value,
                sst_fraction=sst_fraction_value,
                hub_height_m=hub_height_value,
                wind_loss_fraction=wind_loss_value,
                wind_cut_in=wind_cut_in_value,
                wind_rated=wind_rated_value,
                wind_cut_out=wind_cut_out_value,
                energy_cache=energy_cache,
                wind_resource_cache=wind_resource_cache,
                energy_cache_locks=energy_cache_locks,
                wind_resource_cache_locks=wind_resource_cache_locks,
                cache_locks_guard=cache_locks_guard,
                energy_calculator=energy_calculator,
                wind_calculator=wind_calculator,
                workers=worker_value,
                write_debug_scale_results=debug_value,
            )
        )

    for case in _optimization_cases(cases):
        output_files.update(
            _run_country_growth_load_shift_outputs(
                output_path=output_path,
                allocations=city_scale_allocations,
                cooling=str(case["cooling_type"]),
                objectives=tuple(case["optimization_objectives"]),
                workload_file=workload_value,
                idle_power_fraction=idle_power_value,
                hours=hours_value,
                start_time=start_time_value,
                time_alignment=time_alignment_value,
                max_carbon_gap_hours=max_carbon_gap_value,
                sst_fraction=sst_fraction_value,
                grid_import_limit_mw=grid_import_value,
                load_shift_fraction=load_shift_fraction_value,
                hub_height_m=hub_height_value,
                wind_loss_fraction=wind_loss_value,
                wind_cut_in=wind_cut_in_value,
                wind_rated=wind_rated_value,
                wind_cut_out=wind_cut_out_value,
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
                workers=worker_value,
                write_debug_scale_results=debug_value,
                write_baseline_outputs=False,
            )
        )

    for label, path in output_files.items():
        print(f"{label}: {path}")
    return output_files


def _load_run_config(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError(f"Config file must contain one JSON object: {path}")
    return config


def _config_override(config: dict[str, object], key: str, override: object, default: object) -> object:
    if override is not None:
        return override
    return config[key] if key in config else default


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_run_cases(raw_cases: object) -> tuple[dict[str, object], ...]:
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("Config must contain a non-empty 'cases' list.")

    normalized_cases: list[dict[str, object]] = []
    for index, raw_case in enumerate(raw_cases, start=1):
        if not isinstance(raw_case, dict):
            raise ValueError(f"Config case #{index} must be an object.")
        case = dict(raw_case)
        cooling_type = _normalize_cooling_type(case.get("cooling_type", case.get("cooling", "")))
        method = _normalize_optimization_method(case.get("optimization_method"))
        if "optimization_enabled" in case:
            optimization_enabled = _parse_bool(case["optimization_enabled"], label=f"case #{index} optimization_enabled")
            if optimization_enabled and method == "baseline":
                method = "load_shift"
        else:
            optimization_enabled = method != "baseline"

        if not optimization_enabled:
            method = "baseline"
            objectives: tuple[str, ...] = ()
        elif method == "load_shift":
            objectives = _case_objectives(case, index)
        else:
            raise ValueError(f"Config case #{index} has unsupported optimization method: {method!r}")

        normalized_cases.append(
            {
                "name": _optional_text(case.get("name")) or f"case_{index}",
                "cooling_type": cooling_type,
                "optimization_enabled": optimization_enabled,
                "optimization_method": method,
                "optimization_objectives": objectives,
            }
        )
    return tuple(normalized_cases)


def _case_objectives(case: dict[str, object], index: int) -> tuple[str, ...]:
    if "optimization_objectives" in case:
        raw_objectives = case["optimization_objectives"]
    elif "objectives" in case:
        raw_objectives = case["objectives"]
    else:
        raw_objectives = case.get("optimization_objective", case.get("objective", "min-grid-co2"))

    if isinstance(raw_objectives, (list, tuple)):
        objectives = tuple(_normalize_optimization_objective(objective) for objective in raw_objectives)
    else:
        objectives = (_normalize_optimization_objective(raw_objectives),)
    if not objectives:
        raise ValueError(f"Config case #{index} must specify at least one optimization objective.")
    return objectives


def _baseline_cooling_types(cases: tuple[dict[str, object], ...]) -> tuple[str, ...]:
    cooling_types: list[str] = []
    for case in cases:
        if bool(case["optimization_enabled"]):
            continue
        cooling_type = str(case["cooling_type"])
        if cooling_type not in cooling_types:
            cooling_types.append(cooling_type)
    return tuple(cooling_types)


def _optimization_cases(cases: tuple[dict[str, object], ...]) -> tuple[dict[str, object], ...]:
    return tuple(case for case in cases if bool(case["optimization_enabled"]))


def _parse_bool(value: object, *, label: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"{label} must be a boolean value; got {value!r}.")


def _normalize_optimization_method(value: object) -> str:
    if value is None or str(value).strip() == "":
        return "baseline"
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "none": "baseline",
        "no": "baseline",
        "baseline": "baseline",
        "loadshift": "load_shift",
        "load_shift": "load_shift",
    }
    if normalized not in aliases:
        raise ValueError("optimization_method must be 'baseline' or 'load_shift'.")
    return aliases[normalized]


def _normalize_optimization_objective(value: object) -> str:
    normalized = str(value).strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "co2": "min-grid-co2",
        "carbon": "min-grid-co2",
        "carbon-emissions": "min-grid-co2",
        "min-co2": "min-grid-co2",
        "min-grid-co2": "min-grid-co2",
        "mwh": "min-grid-mwh",
        "energy": "min-grid-mwh",
        "min-mwh": "min-grid-mwh",
        "min-grid-mwh": "min-grid-mwh",
    }
    if normalized not in aliases:
        raise ValueError("optimization objective must be 'co2', 'mwh', 'min-grid-co2', or 'min-grid-mwh'.")
    return aliases[normalized]


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
    cooling_types: Iterable[str] = COOLING_TYPES,
    workload_file: str | Path,
    idle_power_fraction: float,
    hours: int | None,
    start_time: str | None,
    time_alignment: str | None,
    max_carbon_gap_hours: int,
    sst_fraction: float,
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
    cooling_type_tuple = tuple(_normalize_cooling_type(cooling_type) for cooling_type in cooling_types)
    cooling_city_scale = run_cooling_comparisons(
        allocations=allocations,
        cooling_types=cooling_type_tuple,
        workload_file=workload_file,
        idle_power_fraction=idle_power_fraction,
        hours=hours,
        start_time=start_time,
        time_alignment=_resolve_baseline_alignment(start_time, time_alignment),
        max_carbon_gap_hours=max_carbon_gap_hours,
        sst_fraction=sst_fraction,
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
    files = write_cooling_output_tables(
        cooling_city_results,
        output_path,
        hours=hours,
        country_metric_aggregation="mean",
        default_growth_scenario="baseline",
        cooling_types=cooling_type_tuple,
    )
    _print_cooling_issue_summary(cooling_city_scale, context="after cooling calculations")
    if write_debug_scale_results:
        debug_path = output_path / f"debug_cooling_scale_{_hours_token(hours)}.csv"
        _write_csv(cooling_city_scale, debug_path)
        files["debug_cooling_scale_csv"] = debug_path
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
    sst_fraction: float,
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
    write_baseline_outputs: bool = True,
) -> dict[str, Path]:
    cooling = _normalize_cooling_type(cooling)
    objectives = tuple(_normalize_optimization_objective(objective) for objective in objectives)
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
        sst_fraction=sst_fraction,
        grid_import_limit_mw=grid_import_limit_mw,
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
            [*COOLING_METRICS, *OPTIMIZATION_RESULT_METRICS],
            extra_group_columns=["objective", "optimization_scenario", "optimization_scenario_label", "cooling_type"],
        )
    )
    if not write_baseline_outputs and "optimization_scenario" in optimization_city_results:
        optimization_city_results = optimization_city_results[
            optimization_city_results["optimization_scenario"] != "baseline"
        ].copy()
    files = write_optimization_output_tables(
        optimization_city_results,
        output_path,
        hours=hours,
        country_metric_aggregation="mean",
        default_growth_scenario="baseline",
    )
    if write_debug_scale_results:
        debug_path = output_path / f"debug_load_shift_scale_{_hours_token(hours)}.csv"
        _write_csv(optimization_city_scale, debug_path)
        files["debug_load_shift_scale_csv"] = debug_path
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
    cooling_types: Iterable[str] = COOLING_TYPES,
    workload_file: str | Path,
    idle_power_fraction: float,
    hours: int | None,
    start_time: str | None,
    time_alignment: str | None,
    max_carbon_gap_hours: int,
    sst_fraction: float,
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
    cooling_type_tuple = tuple(_normalize_cooling_type(cooling_type) for cooling_type in cooling_types)
    if not cooling_type_tuple:
        raise ValueError("At least one cooling_type must be configured.")
    cache_guard = cache_locks_guard or threading.Lock()
    energy_locks = energy_cache_locks if energy_cache_locks is not None else {}
    wind_locks = wind_resource_cache_locks if wind_resource_cache_locks is not None else {}
    tasks: list[tuple[int, int, int, dict[str, object], str]] = []
    allocation_rows = allocations.to_dict(orient="records")
    total_allocations = len(allocation_rows)
    for allocation_index, allocation in enumerate(allocation_rows, start=1):
        for cooling_type in cooling_type_tuple:
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
            sst_fraction=sst_fraction,
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
                sst_fraction=sst_fraction,
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
    scenario_configs: tuple[dict[str, object], ...],
    workload_file: str | Path,
    idle_power_fraction: float,
    hours: int | None,
    start_time: str | None,
    time_alignment: str | None,
    max_carbon_gap_hours: int,
    sst_fraction: float,
    grid_import_limit_mw: float | None,
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
    cooling = _normalize_cooling_type(cooling)
    objectives = tuple(_normalize_optimization_objective(objective) for objective in objectives)
    cache_guard = cache_locks_guard or threading.Lock()
    energy_locks = energy_cache_locks if energy_cache_locks is not None else {}
    wind_locks = wind_resource_cache_locks if wind_resource_cache_locks is not None else {}
    required_locks = required_wind_cache_locks if required_wind_cache_locks is not None else {}
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
            sst_fraction=sst_fraction,
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
                sst_fraction=sst_fraction,
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
                battery_capacity_mwh=0.0,
                battery_roundtrip_efficiency=1.0,
                grid_import_limit_mw=grid_import_limit_mw,
                battery_charge_limit_mw=0.0,
                battery_discharge_limit_mw=0.0,
                load_shift_fraction=scenario_config["load_shift_fraction"],
                hours=hours,
                start_time=start_time,
                time_alignment=time_alignment,
                max_carbon_gap_hours=max_carbon_gap_hours,
                sst_fraction=sst_fraction,
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
            "load_shift_fraction": 0.0,
        },
        {
            "scenario": "load_shift",
            "cooling_type": cooling,
            "load_shift_enabled": True,
            "load_shift_fraction": load_shift_fraction,
        },
    )


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
            sst_fraction=context.sst_fraction,
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
            sst_fraction=context.sst_fraction,
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
            battery_capacity_mwh=0.0,
            battery_roundtrip_efficiency=1.0,
            grid_import_limit_mw=context.grid_import_limit_mw,
            battery_charge_limit_mw=0.0,
            battery_discharge_limit_mw=0.0,
            load_shift_fraction=scenario_config["load_shift_fraction"],
            hours=context.hours,
            start_time=context.start_time,
            time_alignment=context.time_alignment,
            max_carbon_gap_hours=context.max_carbon_gap_hours,
            sst_fraction=context.sst_fraction,
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
    sst_fraction: float,
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
        round(float(sst_fraction), 9),
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
                    sst_fraction=sst_fraction,
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
    sst_fraction: float,
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
        round(float(sst_fraction), 9),
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
                    sst_fraction=sst_fraction,
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
                    server_energy_kwh=energy.it_energy_kwh,
                    server_carbon_emissions_kgco2=energy.it_carbon_emissions_kgco2,
                    cooling_energy_kwh=energy.cooling_energy_kwh,
                    cooling_carbon_emissions_kgco2=energy.cooling_carbon_emissions_kgco2,
                    total_energy_kwh=energy.total_energy_kwh,
                    total_carbon_emissions_kgco2=energy.carbon_emissions_kgco2,
                    datacenter_total_energy_mwh=datacenter_total_energy_mwh,
                    required_wind_capacity_mw=required_wind_capacity_mw,
                    wind_annual_generation_mwh=required_wind_capacity_mw * wind_resource.wind_generation_per_mw_mwh,
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


def _normalize_cooling_type(value: object) -> str:
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "air": "air_source",
        "airsource": "air_source",
        "air_source": "air_source",
        "air_source_heat_pump": "air_source",
        "ashp": "air_source",
        "seawater": "seawater",
        "sea_water": "seawater",
        "seawater_source": "seawater",
        "seawater_source_heat_pump": "seawater",
        "swhp": "seawater",
    }
    if normalized not in aliases:
        raise ValueError("cooling_type must be 'air_source' or 'seawater'.")
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
        if key not in OPTIMIZATION_HOURLY_RESULT_KEYS
        and key != "csv_files"
        and not str(key).startswith("battery_")
        and "_battery_" not in str(key)
    }
    row.update(
        {
            "status": "ok",
            "error_message": "",
            "hours": wind_capacity.hours,
            "rated_it_power_kw_per_facility": wind_capacity.rated_it_power_kw,
            "server_energy_kwh": wind_capacity.server_energy_kwh * multiplier,
            "server_carbon_emissions_kgco2": wind_capacity.server_carbon_emissions_kgco2 * multiplier,
            "cooling_energy_kwh": wind_capacity.cooling_energy_kwh * multiplier,
            "cooling_carbon_emissions_kgco2": wind_capacity.cooling_carbon_emissions_kgco2 * multiplier,
            "total_energy_kwh": wind_capacity.total_energy_kwh * multiplier,
            "total_carbon_emissions_kgco2": wind_capacity.total_carbon_emissions_kgco2 * multiplier,
            "point_id": wind_capacity.point_id,
            "wind_nc_file": wind_capacity.wind_nc_file,
            "wind_generation_per_mw_mwh": wind_capacity.wind_generation_per_mw_mwh,
            "wind_mean_net_capacity_factor": wind_capacity.mean_net_capacity_factor,
            "wind_start_time": wind_capacity.wind_start_time,
            "wind_end_time": wind_capacity.wind_end_time,
        }
    )
    row.update(clean_result)
    row["required_wind_capacity_mw"] = wind_capacity.required_wind_capacity_mw * multiplier
    row["datacenter_total_energy_mwh"] = wind_capacity.datacenter_total_energy_mwh * multiplier
    row["wind_annual_generation_mwh"] = wind_capacity.wind_annual_generation_mwh * multiplier
    _scale_optimization_metrics(row, multiplier)
    if "wind_coverage_mwh" not in row or pd.isna(row.get("wind_coverage_mwh")):
        demand = float(row.get("annual_demand_mwh", row.get("datacenter_total_energy_mwh", 0.0)) or 0.0)
        grid = float(row.get("grid_purchase_mwh", 0.0) or 0.0)
        row["wind_coverage_mwh"] = demand - grid
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
    for metric in COOLING_METRICS:
        row[metric] = 0.0
    for metric in OPTIMIZATION_RESULT_METRICS:
        row[metric] = 0.0
    return row


def _failed_row(row: dict[str, object], error_message: str) -> dict[str, object]:
    row["status"] = "failed"
    row["error_message"] = error_message
    return row


def _write_foundation_outputs(
    output_path: Path,
    country_growths: pd.DataFrame,
    city_scale_allocations: pd.DataFrame,
) -> dict[str, Path]:
    files = {
        "country_growths_csv": output_path / "country_growths.csv",
        "city_scale_allocations_csv": output_path / "city_scale_allocations.csv",
    }
    _write_csv(country_growths, files["country_growths_csv"])
    _write_csv(city_scale_allocations, files["city_scale_allocations_csv"])
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
        description="Run configured country-growth cooling and optimization cases."
    )
    parser.add_argument("--config-file", default=str(DEFAULT_CONFIG_FILE), help="Path to the run.py text config file.")
    parser.add_argument("--manifest-file", default=None, help="Override manifest_file from the config.")
    parser.add_argument("--output-dir", default=None, help="Override output_dir from the config.")
    parser.add_argument("--workload-file", default=None, help="Override workload_file from the config.")
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
        "--write-debug-scale-results",
        action="store_true",
        help="Write scale-level cooling and optimization debug CSVs in addition to all-scale paper outputs.",
    )
    parser.add_argument("--idle-power-fraction", type=float, default=None, help="Override idle_power_fraction. sensitivity: 0.1 / default 0.23 / 0.35")
    parser.add_argument("--hours", type=int, default=None, help="Override hours.")
    parser.add_argument("--start-time", default=None, help="Override start_time.")
    parser.add_argument("--time-alignment", choices=["sst", "latest", "start_time"], default=None)
    parser.add_argument("--max-carbon-gap-hours", type=int, default=None)
    parser.add_argument("--sst-fraction", dest="sst_fraction", type=float, default=None, help="sensitivity: 0.9 / default 1.0 / 1.1")
    parser.add_argument("--grid-import-limit-mw", type=float, default=None)
    parser.add_argument("--load-shift-fraction", type=float, default=None, help="sensitivity:  0.15 / default 0.3 / 0.45")
    parser.add_argument("--hub-height-m", type=float, default=None)
    parser.add_argument("--wind-loss-fraction", type=float, default=None, help="sensitivity:  0.1 / default 0.15 / 0.2")
    parser.add_argument(
        "--optimization-objective",
        "--optimization_objective",
        dest="optimization_objective",
        type=str,
        default=None,
        help="Override optimization objective in optimization cases. sensitivity: default co2 / mwh",
    )
    parser.add_argument("--wind-cut-in", type=float, default=None)
    parser.add_argument("--wind-rated", type=float, default=None)
    parser.add_argument("--wind-cut-out", type=float, default=None)
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            "Number of worker processes for cooling and optimization calculations. "
            "If omitted, the value from the config is used."
        ),
    )
    return parser


if __name__ == "__main__":
    args = _build_arg_parser().parse_args(sys.argv[1:])
    output_files = run_configured_cases(
        config_file=args.config_file,
        manifest_file=args.manifest_file,
        output_dir=args.output_dir,
        workload_file=args.workload_file,
        include_not_ready=True if args.include_not_ready else None,
        dry_run=args.dry_run,
        idle_power_fraction=args.idle_power_fraction,
        hours=args.hours,
        start_time=args.start_time,
        time_alignment=args.time_alignment,
        max_carbon_gap_hours=args.max_carbon_gap_hours,
        sst_fraction=args.sst_fraction,
        grid_import_limit_mw=args.grid_import_limit_mw,
        load_shift_fraction=args.load_shift_fraction,
        optimization_objective=args.optimization_objective,
        hub_height_m=args.hub_height_m,
        wind_loss_fraction=args.wind_loss_fraction,
        wind_cut_in=args.wind_cut_in,
        wind_rated=args.wind_rated,
        wind_cut_out=args.wind_cut_out,
        workers=args.workers,
        countries=args.countries,
        max_countries=args.max_countries,
        write_debug_scale_results=True if args.write_debug_scale_results else None,
    )
    print(json.dumps({key: str(path) for key, path in output_files.items()}, indent=2, ensure_ascii=False))
