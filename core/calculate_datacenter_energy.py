"""Direct data-center energy and emissions calculator.

This module computes hourly IT load, cooling energy, total energy, and carbon
emissions for a selected city using the detailed data-center model in
envs/datacenter.py and the data files in this repository.

Example:
    python calculate_datacenter_energy.py --city "Shanghai" --cooling seawater
    python calculate_datacenter_energy.py --list-cities
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Literal

import numpy as np
import pandas as pd

import core.datacenter as DataCenter
from utils.dc_config_reader import DC_Config


CoolingType = Literal["air_source", "seawater"]


ROOT_DIR = Path(__file__).resolve().parent.parent
DC_CONFIG_FILE = ROOT_DIR / "utils" / "dc_config.json"
CITY_MAP_FILE = ROOT_DIR / "data" / "target_city_map.csv"
WORKLOAD_FILE = ROOT_DIR / "data" / "Workload" / "GoogleClusteData_CPU_Data_Hourly_1.csv"
CARBON_INTENSITY_FILE = (
    ROOT_DIR / "data" / "ci_download_toolkit" / "carbon_intensity_electricitymaps.csv"
)
EPW_DIR = ROOT_DIR / "data" / "epw_download_toolkit" / "epw_files"
SST_FILE = (
    ROOT_DIR / "data" / "sst_download_toolkit" / "sea_surface_temperature_2025_openmeteo.csv"
)
DEFAULT_OUTPUT_DIR = ROOT_DIR / "results"


@dataclass(frozen=True)
class DataCenterEnergyResult:
    city: str
    cooling_type: str
    hours: int
    rated_it_power_kw: float
    idle_power_fraction: float
    it_energy_kwh: float
    cooling_energy_kwh: float
    total_energy_kwh: float
    carbon_emissions_kgco2: float
    carbon_emissions_tco2: float
    average_it_power_kw: float
    average_cooling_power_kw: float
    average_total_power_kw: float
    average_cop: float
    average_pue: float
    min_source_temperature_c: float
    mean_source_temperature_c: float
    max_source_temperature_c: float
    free_cooling_hours: float
    hybrid_cooling_hours: float
    mechanical_cooling_hours: float
    average_effective_cop: float
    average_compressor_cop: float
    seawater_pump_energy_kwh: float
    chilled_water_pump_energy_kwh: float
    compressor_energy_kwh: float
    heat_exchanger_aux_energy_kwh: float
    unmet_cooling_energy_kwh: float
    constraint_violation_hours: float


def list_available_cities(city_map_file: Path = CITY_MAP_FILE) -> list[str]:
    """Return city names from data/target_city_map.csv."""
    city_map = pd.read_csv(city_map_file)
    return city_map["City / metro"].dropna().astype(str).tolist()


def save_result_csv(
    result: DataCenterEnergyResult,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    progress: bool = True,
) -> Path:
    """Save one calculation result as a CSV file under results/.

    The filename includes city, cooling type, and rated IT power.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    city_token = _filename_token(result.city)
    power_token = _format_power_token(result.rated_it_power_kw)
    filename = f"datacenter_energy_{city_token}_{result.cooling_type}_{power_token}.csv"
    csv_path = output_path / filename

    _print_progress("Writing result CSV.", enabled=progress)
    pd.DataFrame([asdict(result)]).to_csv(csv_path, index=False, encoding="utf-8-sig")
    return csv_path


def calculate_data_center_energy(
    city: str,
    cooling_type: CoolingType = "air_source",
    workload_file: str | Path = WORKLOAD_FILE,
    rated_it_power_kw: float = 1000.0,
    idle_power_fraction: float = 0.3,
    supply_temperature_c: float = 18.0,
    u_train: float = 0.8,
    u_infer: float = 0.8,
    r_train: float = 0.9,
    r_infer: float = 0.5,
    p_infer: float = 0.7,
    max_power_fraction: float = 0.88,
    hours: int | None = None,
    progress: bool = True,
) -> DataCenterEnergyResult:
    """Calculate annual or partial-period data-center energy and emissions.

    Args:
        city: City name from data/target_city_map.csv, for example "Shanghai".
        cooling_type: "air_source" or "seawater".
        workload_file: CSV file containing a numeric 'cpu_load' column.
        rated_it_power_kw: IT load at full utilization in kW.
        idle_power_fraction: Fraction of rated IT power used at zero CPU load.
        cooling_load_fraction: Deprecated; retained for API compatibility.
        supply_temperature_c: Chilled-water or supply-side target temperature.
        condenser_approach_c: Deprecated; retained for API compatibility.
        heat_exchanger_approach_c: Deprecated; retained for API compatibility.
        carnot_efficiency: Deprecated; retained for API compatibility.
        min_cop: Deprecated; retained for API compatibility.
        max_cop: Deprecated; retained for API compatibility.
        seawater_min_cop: Deprecated; retained for API compatibility.
        seawater_max_cop: Deprecated; retained for API compatibility.
        seawater_aux_power_fraction: Deprecated; retained for API compatibility.
        hours: Optional number of leading hours to evaluate. Defaults to the
            shortest available length across required hourly series.
        progress: Print stage progress to stderr. JSON output remains on stdout.

    Returns:
        DataCenterEnergyResult with kWh and CO2 summary values from the
        envs/datacenter.py IT and HVAC model.
    """
    _print_progress("Validating city and cooling type.", enabled=progress)
    city = _validate_city(city)
    cooling_type = _normalize_cooling_type(cooling_type)

    _print_progress("Reading workload data.", enabled=progress)
    workload = _read_workload(workload_file)
    _print_progress("Reading carbon-intensity data.", enabled=progress)
    carbon_intensity = _read_city_column(CARBON_INTENSITY_FILE, city, "carbon intensity")
    _print_progress("Reading EPW dry-bulb temperature data.", enabled=progress)
    ambient_temperature = _read_epw_dry_bulb_temperature(city)

    if cooling_type == "air_source":
        _print_progress("Using EPW dry-bulb temperature as cooling source temperature.", enabled=progress)
        source_temperature = ambient_temperature
    else:
        _print_progress("Reading sea-surface temperature data.", enabled=progress)
        source_temperature = _read_city_column(SST_FILE, city, "sea surface temperature")

    _print_progress("Aligning hourly input series.", enabled=progress)
    n_hours = _resolve_hours(hours, workload, carbon_intensity, ambient_temperature, source_temperature)
    workload = workload[:n_hours]
    carbon_intensity = carbon_intensity[:n_hours]
    ambient_temperature = ambient_temperature[:n_hours]
    source_temperature = source_temperature[:n_hours]
    utilization_level = p_infer * u_infer * r_infer + (1-p_infer) * u_train * r_train  # 0.579

    # it_power_kw = rated_it_power_kw * (
    #                (idle_power_fraction + (max_power_fraction - idle_power_fraction) * utilization_level))  # 0.60635

    seawater_temperature = source_temperature if cooling_type == "seawater" else None
    simulation = _simulate_datacenter_energy_with_env_model(
        workload=workload,
        ambient_temperature_c=ambient_temperature,
        seawater_temperature_c=seawater_temperature,
        rated_it_power_kw=rated_it_power_kw,
        cooling_type=cooling_type,
        crac_setpoint_c=supply_temperature_c,
        progress=progress,
    )
    it_power_kw = simulation["it_power_kw"]
    cooling_power_kw = simulation["cooling_power_kw"]
    cop = simulation["cooling_cop"]

    _print_progress("Aggregating energy and emissions results.", enabled=progress)
    total_power_kw = it_power_kw + cooling_power_kw
    # Carbon-intensity data is interpreted as gCO2/kWh.
    emissions_kgco2 = float(np.nansum(total_power_kw * carbon_intensity / 1000.0))
    it_energy_kwh = float(np.nansum(it_power_kw))
    cooling_energy_kwh = float(np.nansum(cooling_power_kw))
    total_energy_kwh = float(np.nansum(total_power_kw))

    return DataCenterEnergyResult(
        city=city,
        cooling_type=cooling_type,
        hours=n_hours,
        rated_it_power_kw=float(rated_it_power_kw),
        idle_power_fraction=float(idle_power_fraction),
        it_energy_kwh=it_energy_kwh,
        cooling_energy_kwh=cooling_energy_kwh,
        total_energy_kwh=total_energy_kwh,
        carbon_emissions_kgco2=emissions_kgco2,
        carbon_emissions_tco2=emissions_kgco2 / 1000.0,
        average_it_power_kw=float(np.nanmean(it_power_kw)),
        average_cooling_power_kw=float(np.nanmean(cooling_power_kw)),
        average_total_power_kw=float(np.nanmean(total_power_kw)),
        average_cop=float(_finite_mean(cop)),
        average_pue=float(total_energy_kwh / it_energy_kwh) if it_energy_kwh else math.inf,
        min_source_temperature_c=float(np.nanmin(source_temperature)),
        mean_source_temperature_c=float(np.nanmean(source_temperature)),
        max_source_temperature_c=float(np.nanmax(source_temperature)),
        free_cooling_hours=float(simulation["free_cooling_hours"]),
        hybrid_cooling_hours=float(simulation["hybrid_cooling_hours"]),
        mechanical_cooling_hours=float(simulation["mechanical_cooling_hours"]),
        average_effective_cop=float(_finite_mean(simulation["seawater_effective_cop"])),
        average_compressor_cop=float(_finite_mean(simulation["seawater_compressor_cop"])),
        seawater_pump_energy_kwh=float(np.nansum(simulation["seawater_pump_power_w"]) / 1000.0),
        chilled_water_pump_energy_kwh=float(np.nansum(simulation["chilled_water_pump_power_w"]) / 1000.0),
        compressor_energy_kwh=float(np.nansum(simulation["compressor_power_w"]) / 1000.0),
        heat_exchanger_aux_energy_kwh=float(np.nansum(simulation["heat_exchanger_aux_power_w"]) / 1000.0),
        unmet_cooling_energy_kwh=float(np.nansum(simulation["unmet_cooling_load_w"]) / 1000.0),
        constraint_violation_hours=float(np.nansum(simulation["constraint_violation"])),
    )


def _validate_city(city: str) -> str:
    city = str(city).strip()
    cities = list_available_cities()
    if city in cities:
        return city

    matches = [candidate for candidate in cities if candidate.lower() == city.lower()]
    if matches:
        return matches[0]

    preview = ", ".join(cities[:20])
    raise ValueError(
        f"Unknown city: {city!r}. Use one of the cities in {CITY_MAP_FILE}. "
        f"First available cities: {preview}"
    )


def _normalize_cooling_type(cooling_type: str) -> CoolingType:
    normalized = str(cooling_type).strip().lower().replace("-", "_")
    aliases = {
        "air": "air_source",
        "air_source": "air_source",
        "ashp": "air_source",
        "conventional": "air_source",
        "seawater": "seawater",
        "sea_water": "seawater",
        "water_source": "seawater",
        "swhp": "seawater",
    }
    if normalized not in aliases:
        raise ValueError("cooling_type must be 'air_source' or 'seawater'.")
    return aliases[normalized]  # type: ignore[return-value]


def _read_workload(workload_file: str | Path = WORKLOAD_FILE) -> np.ndarray:
    workload_path = Path(workload_file)
    if not workload_path.is_absolute():
        workload_path = ROOT_DIR / workload_path
    workload_df = pd.read_csv(workload_path)
    if "cpu_load" not in workload_df.columns:
        raise ValueError(f"{workload_path} must contain a 'cpu_load' column.")
    workload = pd.to_numeric(workload_df["cpu_load"], errors="coerce").to_numpy(dtype=float)
    workload = _fill_missing(workload, "cpu_load")
    return np.clip(workload, 0.0, 1.0)


def _read_city_column(filename: Path, city: str, data_name: str) -> np.ndarray:
    data = pd.read_csv(filename)
    if city not in data.columns:
        raise ValueError(
            f"{data_name} file {filename} does not contain a column for {city!r}. "
            "For seawater cooling, choose a coastal city with sea-temperature data."
        )
    values = pd.to_numeric(data[city], errors="coerce").to_numpy(dtype=float)
    return _fill_missing(values, f"{data_name} for {city}")


def _read_epw_dry_bulb_temperature(city: str) -> np.ndarray:
    epw_file = _find_epw_file(city)
    rows = pd.read_csv(epw_file, skiprows=8, header=None)
    if rows.shape[1] <= 6:
        raise ValueError(f"EPW file {epw_file} does not contain dry-bulb temperature data.")
    dry_bulb = pd.to_numeric(rows.iloc[:, 6], errors="coerce").to_numpy(dtype=float)
    return _fill_missing(dry_bulb, f"EPW dry-bulb temperature for {city}")


def _find_epw_file(city: str) -> Path:
    city_map = pd.read_csv(CITY_MAP_FILE)
    match = city_map.loc[city_map["City / metro"] == city].index
    if len(match) == 0:
        raise ValueError(f"Could not find {city!r} in {CITY_MAP_FILE}.")

    city_index_prefix = f"{int(match[0]) + 1:03d}_"
    matching_files = sorted(EPW_DIR.glob(f"{city_index_prefix}*.epw"))
    if not matching_files:
        raise FileNotFoundError(
            f"Could not find an EPW file for {city!r} with prefix {city_index_prefix} in {EPW_DIR}."
        )
    return matching_files[0]


def _resolve_hours(
    requested_hours: int | None,
    *series: Iterable[float],
) -> int:
    max_available = min(len(values) for values in series)
    if requested_hours is None:
        return max_available
    if requested_hours <= 0:
        raise ValueError("hours must be positive.")
    if requested_hours > max_available:
        raise ValueError(
            f"Requested {requested_hours} hours, but only {max_available} aligned hours are available."
        )
    return requested_hours


def _simulate_datacenter_energy_with_env_model(
    workload: np.ndarray,
    ambient_temperature_c: np.ndarray,
    seawater_temperature_c: np.ndarray | None,
    rated_it_power_kw: float,
    cooling_type: CoolingType,
    crac_setpoint_c: float,
    progress: bool = True,
) -> dict[str, np.ndarray]:
    """Use envs/datacenter.py's detailed IT and HVAC model for hourly energy."""
    _print_progress("Building and calibrating detailed data-center configuration.", enabled=progress)
    dc_config = _build_scaled_dc_config(
        rated_it_power_kw=rated_it_power_kw,
        cooling_type=cooling_type,
        ambient_temperature_c=ambient_temperature_c,
        crac_setpoint_c=crac_setpoint_c,
        progress=progress,
    )
    _print_progress("Initializing detailed IT and HVAC model.", enabled=progress)
    dc_model = DataCenter.DataCenter_ITModel(
        num_racks=dc_config.NUM_RACKS,
        rack_supply_approach_temp_list=dc_config.RACK_SUPPLY_APPROACH_TEMP_LIST,
        rack_CPU_config=dc_config.RACK_CPU_CONFIG,
        max_W_per_rack=dc_config.MAX_W_PER_RACK,
        DC_ITModel_config=dc_config,
    )

    it_power_kw = np.zeros_like(workload, dtype=float)
    cooling_power_kw = np.zeros_like(workload, dtype=float)
    cooling_cop = np.full_like(workload, np.nan, dtype=float)
    seawater_effective_cop = np.full_like(workload, np.nan, dtype=float)
    seawater_compressor_cop = np.full_like(workload, np.nan, dtype=float)
    seawater_pump_power_w = np.zeros_like(workload, dtype=float)
    chilled_water_pump_power_w = np.zeros_like(workload, dtype=float)
    compressor_power_w = np.zeros_like(workload, dtype=float)
    heat_exchanger_aux_power_w = np.zeros_like(workload, dtype=float)
    unmet_cooling_load_w = np.zeros_like(workload, dtype=float)
    constraint_violation = np.zeros_like(workload, dtype=float)
    cooling_modes: list[str] = []

    _print_progress(f"Starting hourly simulation for {len(workload)} hour(s).", enabled=progress)
    progress_marks = _progress_mark_indices(len(workload), intervals=10)
    for hour_index, cpu_load_fraction in enumerate(workload):
        ite_load_pct = float(np.clip(cpu_load_fraction, 0.0, 1.0) * 100.0)
        ite_load_pct_list = [ite_load_pct for _ in range(dc_config.NUM_RACKS)]
        with contextlib.redirect_stdout(io.StringIO()):
            rackwise_cpu_pwr, rackwise_itfan_pwr, rackwise_outlet_temp = (
                dc_model.compute_datacenter_IT_load_outlet_temp(
                    ITE_load_pct_list=ite_load_pct_list,
                    CRAC_setpoint=crac_setpoint_c,
                )
            )
            avg_crac_return_temp = DataCenter.calculate_avg_CRAC_return_temp(
                rack_return_approach_temp_list=dc_config.RACK_RETURN_APPROACH_TEMP_LIST,
                rackwise_outlet_temp=rackwise_outlet_temp,
            )
            data_center_total_ite_load = sum(rackwise_cpu_pwr) + sum(rackwise_itfan_pwr)
            seawater_temp = (
                None
                if seawater_temperature_c is None
                else float(seawater_temperature_c[hour_index])
            )
            hvac_details = DataCenter.calculate_HVAC_power_detailed(
                CRAC_setpoint=crac_setpoint_c,
                avg_CRAC_return_temp=avg_crac_return_temp,
                ambient_temp=float(ambient_temperature_c[hour_index]),
                data_center_full_load=data_center_total_ite_load,
                DC_Config=dc_config,
                seawater_temp=seawater_temp,
            )

        it_power_kw[hour_index] = data_center_total_ite_load / 1000.0
        cooling_power_kw[hour_index] = hvac_details["selected_hvac_power"] / 1000.0
        if hvac_details["selected_hvac_power"] > 0:
            cooling_cop[hour_index] = (
                hvac_details["CRAC_cooling_load"] / hvac_details["selected_hvac_power"]
            )
        cooling_modes.append(str(hvac_details.get("seawater_cooling_mode", "not_used")))
        seawater_effective_cop[hour_index] = float(hvac_details.get("seawater_effective_cop", math.nan))
        seawater_compressor_cop[hour_index] = float(hvac_details.get("seawater_compressor_cop", math.nan))
        seawater_pump_power_w[hour_index] = float(hvac_details.get("seawater_pump_power", 0.0))
        chilled_water_pump_power_w[hour_index] = float(hvac_details.get("seawater_chilled_water_pump_power_w", 0.0))
        compressor_power_w[hour_index] = float(hvac_details.get("seawater_compressor_power_w", 0.0))
        heat_exchanger_aux_power_w[hour_index] = float(hvac_details.get("seawater_heat_exchanger_aux_power_w", 0.0))
        unmet_cooling_load_w[hour_index] = float(hvac_details.get("seawater_unmet_cooling_load_w", 0.0))
        constraint_violation[hour_index] = 1.0 if hvac_details.get("seawater_constraint_violation", False) else 0.0
        if hour_index in progress_marks:
            completed = hour_index + 1
            percent = completed / len(workload) * 100.0
            _print_progress(
                f"Hourly simulation {completed}/{len(workload)} ({percent:.0f}%).",
                enabled=progress,
            )

    return {
        "it_power_kw": it_power_kw,
        "cooling_power_kw": cooling_power_kw,
        "cooling_cop": cooling_cop,
        "free_cooling_hours": float(sum(mode == "free_cooling" for mode in cooling_modes)),
        "hybrid_cooling_hours": float(sum(mode == "hybrid_cooling" for mode in cooling_modes)),
        "mechanical_cooling_hours": float(sum(mode == "mechanical_heat_pump" for mode in cooling_modes)),
        "seawater_effective_cop": seawater_effective_cop,
        "seawater_compressor_cop": seawater_compressor_cop,
        "seawater_pump_power_w": seawater_pump_power_w,
        "chilled_water_pump_power_w": chilled_water_pump_power_w,
        "compressor_power_w": compressor_power_w,
        "heat_exchanger_aux_power_w": heat_exchanger_aux_power_w,
        "unmet_cooling_load_w": unmet_cooling_load_w,
        "constraint_violation": constraint_violation,
    }


def _build_scaled_dc_config(
    rated_it_power_kw: float,
    cooling_type: CoolingType,
    ambient_temperature_c: np.ndarray,
    crac_setpoint_c: float,
    progress: bool = True,
) -> DC_Config:
    """Create a DC_Config whose rack CPU inventory reaches the requested IT rating."""
    rated_it_power_w = float(rated_it_power_kw) * 1000.0
    if rated_it_power_w <= 0:
        raise ValueError("rated_it_power_kw must be positive.")

    _print_progress("Loading dc_config.json.", enabled=progress)
    dc_config = DC_Config(
        dc_config_file=str(DC_CONFIG_FILE),
        datacenter_capacity_mw=rated_it_power_w / 1e6,
    )
    dc_config.COOLING_SYSTEM_MODE = (
        "seawater" if cooling_type == "seawater" else "conventional_full"
    )
    dc_config.MAX_W_PER_RACK = int(math.ceil(rated_it_power_w / dc_config.NUM_RACKS))
    dc_config.RACK_CPU_CONFIG = _expand_rack_cpu_config_to_capacity(
        dc_config.RACK_CPU_CONFIG,
        dc_config.MAX_W_PER_RACK,
    )
    _print_progress("Calibrating detailed IT capacity.", enabled=progress)
    _calibrate_dc_config_it_capacity(dc_config, rated_it_power_w, crac_setpoint_c)
    max_ambient_temp = float(np.nanmax(ambient_temperature_c))
    _print_progress("Sizing chiller and cooling-tower reference load.", enabled=progress)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            ctafr, ct_rated_load = DataCenter.chiller_sizing(
                dc_config,
                min_CRAC_setpoint=15.0,
                max_CRAC_setpoint=21.6,
                max_ambient_temp=max_ambient_temp,
            )
    except Exception:
        ctafr = dc_config.CT_REFRENCE_AIR_FLOW_RATE
        ct_rated_load = max(dc_config.CT_FAN_REF_P, rated_it_power_w)
    dc_config.CT_REFRENCE_AIR_FLOW_RATE = ctafr
    dc_config.CT_FAN_REF_P = ct_rated_load
    return dc_config


def _print_progress(message: str, enabled: bool = True) -> None:
    if enabled:
        print(f"[progress] {message}", file=sys.stderr, flush=True)


def _progress_mark_indices(total: int, intervals: int = 10) -> set[int]:
    if total <= 0:
        return set()
    return {
        min(total - 1, max(0, math.ceil(total * fraction / intervals) - 1))
        for fraction in range(1, intervals + 1)
    }


def _calibrate_dc_config_it_capacity(
    dc_config: DC_Config,
    target_it_power_w: float,
    crac_setpoint_c: float,
) -> None:
    """Scale server power so the detailed IT model matches target power at 100% load."""
    modeled_cpu_w, modeled_fan_w = _estimate_detailed_it_power_components_w(
        dc_config=dc_config,
        cpu_load_fraction=1.0,
        crac_setpoint_c=crac_setpoint_c,
    )
    if modeled_cpu_w <= 0 or modeled_cpu_w + modeled_fan_w <= 0:
        raise ValueError("Detailed IT model returned non-positive full-load power.")
    if target_it_power_w <= modeled_fan_w:
        raise ValueError(
            "rated_it_power_kw is too small for the detailed model's fixed IT fan power."
        )

    scale = (target_it_power_w - modeled_fan_w) / modeled_cpu_w
    for rack_config in dc_config.RACK_CPU_CONFIG:
        for cpu_config in rack_config:
            cpu_config["full_load_pwr"] = float(cpu_config["full_load_pwr"]) * scale
            cpu_config["idle_pwr"] = float(cpu_config["idle_pwr"]) * scale
    dc_config.MAX_W_PER_RACK = int(math.ceil(dc_config.MAX_W_PER_RACK * scale))


def _estimate_detailed_it_power_components_w(
    dc_config: DC_Config,
    cpu_load_fraction: float,
    crac_setpoint_c: float,
) -> tuple[float, float]:
    """Return detailed-model CPU and IT fan power while suppressing model prints."""
    dc_model = DataCenter.DataCenter_ITModel(
        num_racks=dc_config.NUM_RACKS,
        rack_supply_approach_temp_list=dc_config.RACK_SUPPLY_APPROACH_TEMP_LIST,
        rack_CPU_config=dc_config.RACK_CPU_CONFIG,
        max_W_per_rack=dc_config.MAX_W_PER_RACK,
        DC_ITModel_config=dc_config,
    )
    ite_load_pct_list = [
        float(np.clip(cpu_load_fraction, 0.0, 1.0) * 100.0)
        for _ in range(dc_config.NUM_RACKS)
    ]
    with contextlib.redirect_stdout(io.StringIO()):
        rackwise_cpu_pwr, rackwise_itfan_pwr, _ = (
            dc_model.compute_datacenter_IT_load_outlet_temp(
                ITE_load_pct_list=ite_load_pct_list,
                CRAC_setpoint=crac_setpoint_c,
            )
        )
    return float(sum(rackwise_cpu_pwr)), float(sum(rackwise_itfan_pwr))


def _expand_rack_cpu_config_to_capacity(
    rack_cpu_config: list[list[dict[str, float]]],
    max_w_per_rack: int,
) -> list[list[dict[str, float]]]:
    """Repeat each rack's server template so Rack can fill up to max_w_per_rack."""
    expanded_config: list[list[dict[str, float]]] = []
    for cpu_template in rack_cpu_config:
        if not cpu_template:
            raise ValueError("RACK_CPU_CONFIG contains an empty rack template.")

        repeated: list[dict[str, float]] = []
        total_full_load = 0.0
        template_index = 0
        while total_full_load < max_w_per_rack and template_index < 1_000_000:
            cpu = cpu_template[template_index % len(cpu_template)]
            repeated.append(dict(cpu))
            total_full_load += float(cpu["full_load_pwr"])
            template_index += 1
        expanded_config.append(repeated)

    return expanded_config


def _fill_missing(values: np.ndarray, label: str) -> np.ndarray:
    series = pd.Series(values, dtype="float64")
    if series.notna().sum() == 0:
        raise ValueError(f"{label} contains no numeric values.")
    if series.isna().any():
        series = series.interpolate(limit_direction="both")
    return series.to_numpy(dtype=float)


def _finite_mean(values: np.ndarray) -> float:
    finite_values = np.asarray(values, dtype=float)
    finite_values = finite_values[np.isfinite(finite_values)]
    return float(np.nanmean(finite_values)) if finite_values.size else math.nan


def _format_result(result: DataCenterEnergyResult) -> str:
    return "\n".join(
        [
            f"City: {result.city}",
            f"Cooling type: {result.cooling_type}",
            f"Hours: {result.hours}",
            f"IT energy: {result.it_energy_kwh:,.2f} kWh",
            f"Cooling energy: {result.cooling_energy_kwh:,.2f} kWh",
            f"Total energy: {result.total_energy_kwh:,.2f} kWh",
            f"Carbon emissions: {result.carbon_emissions_tco2:,.3f} tCO2",
            f"Average PUE: {result.average_pue:.3f}",
            f"Average COP: {result.average_cop:.3f}",
            (
                "Source temperature: "
                f"{result.min_source_temperature_c:.2f}/"
                f"{result.mean_source_temperature_c:.2f}/"
                f"{result.max_source_temperature_c:.2f} C "
                "(min/mean/max)"
            ),
        ]
    )


def _filename_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", value.strip())
    return token.strip("_") or "unknown"


def _format_power_token(rated_it_power_kw: float) -> str:
    if float(rated_it_power_kw).is_integer():
        return f"{int(rated_it_power_kw)}kW"
    return f"{rated_it_power_kw:g}kW".replace(".", "p")


def main(args) -> None:
    if args.list_cities:
        for available_city in list_available_cities():
            print(available_city)
        return

    if not args.city:
        parser.error("--city is required unless --list-cities is used.")

    result = calculate_data_center_energy(
        city=args.city,
        cooling_type=args.cooling,
        workload_file=args.workload_file,
        rated_it_power_kw=args.rated_it_power_kw,
        idle_power_fraction=args.idle_power_fraction,
        hours=args.hours,
    )
    csv_path = save_result_csv(result, args.output_dir)
    if args.json:
        payload = asdict(result)
        payload["csv_file"] = str(csv_path)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(_format_result(result))
        print(f"CSV saved to: {csv_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Calculate data-center energy and carbon emissions for a selected city."
    )
    parser.add_argument("--city", help="City name from data/target_city_map.csv.")
    parser.add_argument(
        "--cooling",
        default="air_source",
        choices=["air_source", "seawater", "conventional", "ashp", "swhp"],
        help="Cooling system type. conventional/ashp are aliases for air_source.",
    )
    parser.add_argument(
        "--workload-file",
        default=str(WORKLOAD_FILE),
        help="CSV workload file containing a cpu_load column.",
    )
    parser.add_argument("--rated-it-power-kw", type=float, default=20000.0)
    parser.add_argument("--idle-power-fraction", type=float, default=0.35)
    parser.add_argument("--hours", type=int, default=None)
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for the output CSV. Defaults to results/.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--list-cities", action="store_true", help="Print available city names.")
    args = parser.parse_args()

    main(args)
