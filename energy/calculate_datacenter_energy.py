"""Direct data-center energy and emissions calculator.

This module computes hourly IT load, cooling energy, total energy, and carbon
emissions for a selected city using the detailed data-center model in
energy/datacenter.py and the data files in this repository.

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
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

import numpy as np
import pandas as pd

import energy.datacenter as DataCenter
from utils.dc_config_reader import DC_Config


CoolingType = Literal["air_source", "seawater"]


ROOT_DIR = Path(__file__).resolve().parent.parent
DC_CONFIG_FILE = ROOT_DIR / "utils" / "dc_config.json"
CITY_MAP_FILE = ROOT_DIR / "data" / "coastal_datacenter_city_manifest.xlsx"
CITY_MANIFEST_SHEET = "City_manifest"
WORKLOAD_FILE = ROOT_DIR / "data" / "Workload" / "GoogleClusteData_CPU_Data_Hourly_1.csv"
CARBON_INTENSITY_FILE = (
    ROOT_DIR / "data" / "ci_download_toolkit" / "city_grid_carbon_intensity_electricitymaps_10y.csv"
)
EPW_DIR = ROOT_DIR / "data" / "epw_download_toolkit" / "epw_2025_era5_only"  # "epw_files"
SST_FILE = (
    ROOT_DIR / "data" / "sst_download_toolkit" / "sea_surface_temperature_2025_openmeteo.csv"
)
DEFAULT_OUTPUT_DIR = ROOT_DIR / "results"
XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
XLSX_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _clean_header(value: object) -> str:
    return str(value).replace("\ufeff", "").strip()


def _xlsx_col_index(cell_ref: str) -> int:
    match = re.match(r"([A-Z]+)", cell_ref)
    if not match:
        return 0
    index = 0
    for char in match.group(1):
        index = index * 26 + ord(char) - 64
    return index - 1


def _read_xlsx_sheet_rows(path: Path, sheet_name: str) -> list[dict[str, Any]]:
    """Read a plain worksheet from xlsx using only the standard library."""
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall(XLSX_NS + "si"):
                shared.append("".join(text.text or "" for text in item.iter(XLSX_NS + "t")))

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheets = workbook.find(XLSX_NS + "sheets")
        relationship_id = None
        for sheet in [] if sheets is None else sheets:
            if sheet.attrib.get("name") == sheet_name:
                relationship_id = sheet.attrib[XLSX_REL_ID]
                break
        if relationship_id is None:
            raise ValueError(f"Sheet not found: {sheet_name}")

        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        target = None
        for relationship in relationships:
            if relationship.attrib.get("Id") == relationship_id:
                target = relationship.attrib["Target"].lstrip("/")
                break
        if target is None:
            raise ValueError(f"Worksheet relationship not found for sheet: {sheet_name}")
        if not target.startswith("xl/"):
            target = "xl/" + target

        worksheet = ET.fromstring(archive.read(target))
        rows: list[list[Any]] = []
        for row in worksheet.findall(XLSX_NS + "sheetData/" + XLSX_NS + "row"):
            cells: dict[int, Any] = {}
            max_column = -1
            for cell in row.findall(XLSX_NS + "c"):
                column_index = _xlsx_col_index(cell.attrib.get("r", "A1"))
                cell_type = cell.attrib.get("t")
                value_node = cell.find(XLSX_NS + "v")
                value: Any = None
                if cell_type == "inlineStr":
                    value = "".join(text.text or "" for text in cell.iter(XLSX_NS + "t"))
                elif value_node is not None:
                    raw = value_node.text
                    if cell_type == "s":
                        value = shared[int(raw)]
                    elif cell_type == "b":
                        value = bool(int(raw))
                    else:
                        try:
                            value = float(raw)
                            if value.is_integer():
                                value = int(value)
                        except Exception:
                            value = raw
                cells[column_index] = value
                max_column = max(max_column, column_index)
            rows.append([cells.get(i) for i in range(max_column + 1)])

    if not rows:
        return []
    header = [_clean_header(value) for value in rows[0]]
    output: list[dict[str, Any]] = []
    for row in rows[1:]:
        output.append({
            header[i]: row[i] if i < len(row) else None
            for i in range(len(header))
            if header[i] not in {"", "None"}
        })
    return output


def _is_toolkit_ready(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _manifest_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def load_city_manifest(
    city_map_file: str | Path = CITY_MAP_FILE,
    sheet_name: str = CITY_MANIFEST_SHEET,
) -> pd.DataFrame:
    manifest_path = Path(city_map_file)
    rows = _read_xlsx_sheet_rows(manifest_path, sheet_name)
    if not rows:
        raise ValueError(f"Manifest is empty: {manifest_path}")

    required_columns = [
        "repo_city_index",
        "epw_filename_prefix",
        "country",
        "datacentermap_market",
        "selection_status",
        "toolkit_ready",
    ]
    missing = [column for column in required_columns if column not in rows[0]]
    if missing:
        raise ValueError(
            f"Workbook sheet {sheet_name} is missing required columns: {', '.join(missing)}"
        )

    manifest = pd.DataFrame(rows)
    manifest = manifest[manifest["toolkit_ready"].map(_is_toolkit_ready)].copy()
    manifest["datacentermap_market"] = manifest["datacentermap_market"].map(_manifest_text)
    manifest["country"] = manifest["country"].map(_manifest_text)
    manifest["epw_filename_prefix"] = manifest["epw_filename_prefix"].map(_manifest_text)
    manifest = manifest[manifest["datacentermap_market"] != ""]
    if manifest.empty:
        raise ValueError(f"No toolkit-ready rows found in workbook sheet {sheet_name}: {manifest_path}")
    return manifest.reset_index(drop=True)


@dataclass(frozen=True)
class DataCenterEnergyResult:
    city: str
    cooling_type: str
    hours: int
    simulation_start_time: str | None
    simulation_end_time: str | None
    time_alignment: str
    carbon_intensity_start_time: str | None
    carbon_intensity_end_time: str | None
    sst_start_time: str | None
    sst_end_time: str | None
    rated_it_power_kw: float
    idle_power_fraction: float
    it_energy_kwh: float
    it_carbon_emissions_kgco2: float
    cooling_energy_kwh: float
    cooling_carbon_emissions_kgco2: float
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
    """Return toolkit-ready city names from the coastal data-center manifest."""
    city_map = load_city_manifest(city_map_file)
    return city_map["datacentermap_market"].dropna().astype(str).tolist()


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
    start_time: str | None = None,
    time_alignment: Literal["sst", "latest", "start_time"] | None = None,
    max_carbon_gap_hours: int = 6,
    progress: bool = True,
) -> DataCenterEnergyResult:
    """Calculate annual or partial-period data-center energy and emissions.

    Args:
        city: City name from data/coastal_datacenter_city_manifest.xlsx, for example "Shanghai".
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
        hours: Optional number of hours to evaluate. Defaults to the shortest
            compatible length across required hourly series.
        start_time: Optional simulation start timestamp. When supplied, input
            time series are aligned from this timestamp.
        time_alignment: Optional alignment mode. Defaults to "sst" for
            seawater cooling and "latest" for air-source cooling.
        max_carbon_gap_hours: Maximum consecutive missing carbon-intensity
            hours that may be filled by time interpolation after alignment.
        progress: Print stage progress to stderr. JSON output remains on stdout.

    Returns:
        DataCenterEnergyResult with kWh and CO2 summary values from the
        envs/datacenter.py IT and HVAC model.
    """
    _print_progress("Validating city and cooling type.", enabled=progress)
    city = _validate_city(city)
    cooling_type = _normalize_cooling_type(cooling_type)

    _print_progress("Aligning hourly input series.", enabled=progress)
    aligned_inputs = _resolve_aligned_inputs(
        city=city,
        cooling_type=cooling_type,
        workload_file=workload_file,
        hours=hours,
        start_time=start_time,
        time_alignment=time_alignment,
        max_carbon_gap_hours=max_carbon_gap_hours,
        progress=progress,
    )
    workload = aligned_inputs["workload"]
    carbon_intensity = aligned_inputs["carbon_intensity"]
    ambient_temperature = aligned_inputs["ambient_temperature"]
    source_temperature = aligned_inputs["source_temperature"]
    metadata = aligned_inputs["metadata"]
    n_hours = len(workload)
    # utilization_level = p_infer * u_infer * r_infer + (1-p_infer) * u_train * r_train  # 0.579
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
    it_emissions_kgco2 = float(np.nansum(it_power_kw * carbon_intensity / 1000.0))
    cooling_emissions_kgco2 = float(np.nansum(cooling_power_kw * carbon_intensity / 1000.0))
    emissions_kgco2 = it_emissions_kgco2 + cooling_emissions_kgco2
    it_energy_kwh = float(np.nansum(it_power_kw))
    cooling_energy_kwh = float(np.nansum(cooling_power_kw))
    total_energy_kwh = float(np.nansum(total_power_kw))

    return DataCenterEnergyResult(
        city=city,
        cooling_type=cooling_type,
        hours=n_hours,
        simulation_start_time=metadata["simulation_start_time"],
        simulation_end_time=metadata["simulation_end_time"],
        time_alignment=metadata["time_alignment"],
        carbon_intensity_start_time=metadata["carbon_intensity_start_time"],
        carbon_intensity_end_time=metadata["carbon_intensity_end_time"],
        sst_start_time=metadata["sst_start_time"],
        sst_end_time=metadata["sst_end_time"],
        rated_it_power_kw=float(rated_it_power_kw),
        idle_power_fraction=float(idle_power_fraction),
        it_energy_kwh=it_energy_kwh,
        it_carbon_emissions_kgco2=it_emissions_kgco2,
        cooling_energy_kwh=cooling_energy_kwh,
        cooling_carbon_emissions_kgco2=cooling_emissions_kgco2,
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


def _read_city_timeseries(filename: Path, city: str, data_name: str) -> pd.Series:
    """Read a city column as an hourly timestamp-indexed series.

    Timestamps are normalized to timezone-naive UTC. Duplicate timestamps keep
    the last non-empty city value so regenerated CSV fragments can be appended
    safely before this reader is called.
    """
    data = pd.read_csv(filename)
    if "timestamp" not in data.columns:
        raise ValueError(f"{data_name} file {filename} must contain a 'timestamp' column.")
    if city not in data.columns:
        raise ValueError(
            f"{data_name} file {filename} does not contain a column for {city!r}."
        )

    timestamps = _parse_timestamp_series(data["timestamp"], filename, data_name)
    values = pd.to_numeric(data[city], errors="coerce")
    series_frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "value": values.astype("float64"),
            "_row_order": np.arange(len(data)),
        }
    )
    series_frame = series_frame.dropna(subset=["timestamp"])
    if series_frame.empty:
        raise ValueError(f"{data_name} file {filename} contains no parseable timestamps.")

    series_frame = series_frame.sort_values(["timestamp", "_row_order"])

    def last_non_empty(group: pd.Series) -> float:
        non_empty = group.dropna()
        return float(non_empty.iloc[-1]) if not non_empty.empty else math.nan

    result = series_frame.groupby("timestamp", sort=True)["value"].agg(last_non_empty)
    result.index = pd.DatetimeIndex(result.index, name="timestamp")
    return result.sort_index()


def _read_epw_dry_bulb_temperature(city: str) -> np.ndarray:
    epw_file = _find_epw_file(city)
    rows = pd.read_csv(epw_file, skiprows=8, header=None)
    if rows.shape[1] <= 6:
        raise ValueError(f"EPW file {epw_file} does not contain dry-bulb temperature data.")
    dry_bulb = pd.to_numeric(rows.iloc[:, 6], errors="coerce").to_numpy(dtype=float)
    return _fill_missing(dry_bulb, f"EPW dry-bulb temperature for {city}")


def _resolve_aligned_inputs(
    city: str,
    cooling_type: CoolingType,
    workload_file: str | Path,
    hours: int | None,
    start_time: str | None,
    time_alignment: Literal["sst", "latest", "start_time"] | None,
    max_carbon_gap_hours: int,
    carbon_intensity_file: str | Path = CARBON_INTENSITY_FILE,
    sst_file: str | Path = SST_FILE,
    progress: bool = True,
) -> dict[str, object]:
    """Return workload, weather, SST, and carbon data on one timestamp axis."""
    if max_carbon_gap_hours < 0:
        raise ValueError("max_carbon_gap_hours must be non-negative.")

    _print_progress("Reading workload data.", enabled=progress)
    workload = _read_workload(workload_file)
    _print_progress("Reading carbon-intensity data.", enabled=progress)
    carbon_path = Path(carbon_intensity_file)
    carbon_series = _read_city_timeseries(carbon_path, city, "carbon intensity")
    _print_progress("Reading EPW dry-bulb temperature data.", enabled=progress)
    ambient_epw = _read_epw_dry_bulb_temperature(city)

    alignment = _resolve_time_alignment(cooling_type, time_alignment, start_time)
    sst_series: pd.Series | None = None
    sst_path = Path(sst_file)
    if cooling_type == "seawater" or alignment == "sst":
        _print_progress("Reading sea-surface temperature data.", enabled=progress)
        sst_series = _read_city_timeseries(sst_path, city, "sea surface temperature")

    timestamps = _select_simulation_timestamps(
        city=city,
        cooling_type=cooling_type,
        alignment=alignment,
        hours=hours,
        start_time=start_time,
        workload_length=len(workload),
        carbon_series=carbon_series,
        sst_series=sst_series,
    )

    workload = workload[: len(timestamps)]
    carbon_aligned = _align_carbon_intensity(
        carbon_series=carbon_series,
        timestamps=timestamps,
        city=city,
        filename=carbon_path,
        max_gap_hours=max_carbon_gap_hours,
    )
    ambient_temperature = _map_epw_to_timestamps(ambient_epw, timestamps, city)

    source_series: pd.Series | None = None
    if sst_series is not None:
        source_series = _align_value_series(
            sst_series,
            timestamps,
            city=city,
            filename=sst_path,
            data_name="sea surface temperature",
        )
        sst_start_time = _format_timestamp(source_series.index[0])
        sst_end_time = _format_timestamp(source_series.index[-1])
    else:
        sst_start_time = None
        sst_end_time = None

    if cooling_type == "seawater":
        if source_series is None:
            raise ValueError("Seawater cooling requires sea-surface temperature data.")
        source_temperature = source_series.to_numpy(dtype=float)
    else:
        _print_progress("Using EPW dry-bulb temperature as cooling source temperature.", enabled=progress)
        source_temperature = ambient_temperature

    metadata = {
        "simulation_start_time": _format_timestamp(timestamps[0]),
        "simulation_end_time": _format_timestamp(timestamps[-1]),
        "time_alignment": alignment,
        "carbon_intensity_start_time": _format_timestamp(carbon_aligned.index[0]),
        "carbon_intensity_end_time": _format_timestamp(carbon_aligned.index[-1]),
        "sst_start_time": sst_start_time,
        "sst_end_time": sst_end_time,
    }

    return {
        "timestamps": timestamps,
        "workload": workload,
        "carbon_intensity": carbon_aligned.to_numpy(dtype=float),
        "ambient_temperature": ambient_temperature,
        "source_temperature": source_temperature,
        "metadata": metadata,
    }


def _resolve_time_alignment(
    cooling_type: CoolingType,
    time_alignment: Literal["sst", "latest", "start_time"] | None,
    start_time: str | None,
) -> Literal["sst", "latest", "start_time"]:
    if start_time is not None:
        return "start_time"
    if time_alignment is None:
        return "sst" if cooling_type == "seawater" else "latest"
    if time_alignment not in {"sst", "latest", "start_time"}:
        raise ValueError("time_alignment must be one of: sst, latest, start_time.")
    return time_alignment


def _select_simulation_timestamps(
    city: str,
    cooling_type: CoolingType,
    alignment: Literal["sst", "latest", "start_time"],
    hours: int | None,
    start_time: str | None,
    workload_length: int,
    carbon_series: pd.Series,
    sst_series: pd.Series | None,
) -> pd.DatetimeIndex:
    if alignment == "sst":
        if sst_series is None:
            raise ValueError("SST alignment requires sea-surface temperature data.")
        n_hours = _resolve_requested_hours(hours, workload_length, len(sst_series))
        return pd.DatetimeIndex(sst_series.index[:n_hours])

    if alignment == "latest":
        if cooling_type == "seawater":
            if sst_series is None:
                raise ValueError("Seawater cooling requires SST data for latest alignment.")
            start_bound = max(carbon_series.index.min(), sst_series.index.min())
            end_bound = min(carbon_series.index.max(), sst_series.index.max())
            label = "common carbon/SST"
        else:
            start_bound = carbon_series.index.min()
            end_bound = carbon_series.index.max()
            label = "carbon intensity"
        available_hours = _inclusive_hour_count(start_bound, end_bound)
        n_hours = _resolve_requested_hours(hours, workload_length, available_hours)
        return pd.DatetimeIndex(pd.date_range(end=end_bound, periods=n_hours, freq="h"))

    if alignment == "start_time":
        if not start_time:
            raise ValueError("time_alignment='start_time' requires --start-time.")
        start_timestamp = _parse_timestamp_argument(start_time, "--start-time")
        if cooling_type == "seawater":
            if sst_series is None:
                raise ValueError("Seawater cooling requires SST data for start-time alignment.")
            end_bound = min(carbon_series.index.max(), sst_series.index.max())
            label = "common carbon/SST"
        else:
            end_bound = carbon_series.index.max()
            label = "carbon intensity"
        available_hours = _inclusive_hour_count(start_timestamp, end_bound)
        n_hours = _resolve_requested_hours(hours, workload_length, available_hours)
        end_timestamp = start_timestamp + pd.Timedelta(hours=n_hours - 1)
        if end_timestamp > end_bound:
            raise ValueError(
                f"Requested {n_hours} hour(s) for {city}, but the {label} data only "
                f"extends through {_format_timestamp(end_bound)}."
            )
        return pd.DatetimeIndex(pd.date_range(start=start_timestamp, periods=n_hours, freq="h"))

    raise ValueError(f"Unsupported time alignment mode: {alignment!r}")


def _resolve_requested_hours(
    requested_hours: int | None,
    workload_length: int,
    available_hours: int,
) -> int:
    if requested_hours is not None and requested_hours <= 0:
        raise ValueError("hours must be positive.")
    max_available = min(int(workload_length), int(available_hours))
    if max_available <= 0:
        raise ValueError("No aligned hourly input data are available.")
    if requested_hours is None:
        return max_available
    if requested_hours > max_available:
        raise ValueError(
            f"Requested {requested_hours} hours, but only {max_available} aligned hours are available."
        )
    return int(requested_hours)


def _inclusive_hour_count(start: pd.Timestamp, end: pd.Timestamp) -> int:
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    if pd.isna(start) or pd.isna(end) or end < start:
        return 0
    return int((end - start) / pd.Timedelta(hours=1)) + 1


def _align_carbon_intensity(
    carbon_series: pd.Series,
    timestamps: pd.DatetimeIndex,
    city: str,
    filename: Path,
    max_gap_hours: int,
) -> pd.Series:
    aligned = carbon_series.reindex(timestamps)
    if aligned.notna().sum() == 0:
        raise ValueError(
            f"Carbon intensity for {city!r} in {filename} has no overlap with "
            f"the requested simulation window {_format_time_range(timestamps)}. "
            f"Carbon data range: {_format_time_range(carbon_series.index)}."
        )

    missing_ranges = _missing_ranges(aligned)
    invalid_ranges = [
        item
        for item in missing_ranges
        if item["hours"] > max_gap_hours or item["at_edge"]
    ]
    if invalid_ranges:
        raise ValueError(
            f"Carbon intensity for {city!r} in {filename} is missing aligned "
            f"hour(s) beyond the allowed {max_gap_hours}-hour gap. "
            f"Missing ranges: {_format_missing_ranges(invalid_ranges)}. "
            "Regenerate the carbon-intensity file for the simulation window or "
            "choose a different --start-time/--time-alignment."
        )

    if missing_ranges:
        aligned = aligned.interpolate(
            method="time",
            limit=max_gap_hours,
            limit_area="inside",
        )
    if aligned.isna().any():
        remaining = _missing_ranges(aligned)
        raise ValueError(
            f"Carbon intensity for {city!r} in {filename} still contains missing "
            f"values after interpolation. Missing ranges: {_format_missing_ranges(remaining)}."
        )
    return aligned.astype(float)


def _align_value_series(
    series: pd.Series,
    timestamps: pd.DatetimeIndex,
    city: str,
    filename: Path,
    data_name: str,
) -> pd.Series:
    aligned = series.reindex(timestamps)
    if aligned.notna().sum() == 0:
        raise ValueError(
            f"{data_name} for {city!r} in {filename} has no overlap with "
            f"the requested simulation window {_format_time_range(timestamps)}. "
            f"Data range: {_format_time_range(series.index)}."
        )
    if aligned.isna().any():
        aligned = aligned.interpolate(method="time", limit_direction="both")
    if aligned.isna().any():
        missing = _missing_ranges(aligned)
        raise ValueError(
            f"{data_name} for {city!r} in {filename} contains missing values "
            f"after alignment. Missing ranges: {_format_missing_ranges(missing)}."
        )
    return aligned.astype(float)


def _map_epw_to_timestamps(
    dry_bulb_temperature: np.ndarray,
    timestamps: pd.DatetimeIndex,
    city: str,
) -> np.ndarray:
    dry_bulb = np.asarray(dry_bulb_temperature, dtype=float)
    if len(dry_bulb) < 8760:
        raise ValueError(f"EPW dry-bulb temperature for {city!r} has fewer than 8760 hours.")

    feb_29 = (timestamps.month == 2) & (timestamps.day == 29)
    if bool(feb_29.any()):
        first_bad = timestamps[feb_29][0]
        raise ValueError(
            f"EPW dry-bulb mapping for {city!r} received leap-day timestamp "
            f"{_format_timestamp(first_bad)}. This project assumes non-leap 8760-hour years."
        )

    epw_index = (timestamps.dayofyear.to_numpy(dtype=int) - 1) * 24 + timestamps.hour.to_numpy(dtype=int)
    if np.any(epw_index < 0) or np.any(epw_index >= 8760):
        raise ValueError(
            f"EPW dry-bulb mapping for {city!r} produced indices outside a non-leap 8760-hour year."
        )
    return dry_bulb[epw_index]


def _parse_timestamp_series(values: pd.Series, filename: Path, data_name: str) -> pd.Series:
    parsed = _parse_datetime_utc(values)
    if parsed.notna().sum() == 0:
        raise ValueError(f"{data_name} file {filename} contains no parseable timestamps.")
    return parsed.dt.tz_convert(None)


def _parse_timestamp_argument(value: str, label: str) -> pd.Timestamp:
    parsed = _parse_datetime_utc(pd.Series([value]))
    if parsed.isna().iloc[0]:
        raise ValueError(f"{label} must be a parseable timestamp, got {value!r}.")
    return pd.Timestamp(parsed.dt.tz_convert(None).iloc[0])


def _parse_datetime_utc(values: pd.Series) -> pd.Series:
    try:
        parsed = pd.to_datetime(values, errors="coerce", utc=True, format="mixed")
    except (TypeError, ValueError):
        parsed = pd.to_datetime(values, errors="coerce", utc=True)

    parsed = pd.Series(parsed, index=values.index)
    if parsed.notna().sum() < values.notna().sum():
        parsed = values.map(
            lambda value: pd.to_datetime(value, errors="coerce", utc=True)
            if pd.notna(value)
            else pd.NaT
        )
        parsed = pd.Series(pd.to_datetime(parsed, errors="coerce", utc=True), index=values.index)
    return parsed


def _missing_ranges(series: pd.Series) -> list[dict[str, object]]:
    missing = series.isna().to_numpy()
    ranges: list[dict[str, object]] = []
    start_index: int | None = None
    for index, is_missing in enumerate(missing):
        if is_missing and start_index is None:
            start_index = index
        if start_index is not None and (not is_missing or index == len(missing) - 1):
            end_index = index - 1 if not is_missing else index
            ranges.append(
                {
                    "start": series.index[start_index],
                    "end": series.index[end_index],
                    "hours": end_index - start_index + 1,
                    "at_edge": start_index == 0 or end_index == len(missing) - 1,
                }
            )
            start_index = None
    return ranges


def _format_missing_ranges(ranges: list[dict[str, object]], limit: int = 5) -> str:
    if not ranges:
        return "none"
    formatted = [
        f"{_format_timestamp(item['start'])} to {_format_timestamp(item['end'])} "
        f"({item['hours']}h)"
        for item in ranges[:limit]
    ]
    if len(ranges) > limit:
        formatted.append(f"... and {len(ranges) - limit} more")
    return "; ".join(formatted)


def _format_time_range(index: pd.Index) -> str:
    if len(index) == 0:
        return "empty"
    return f"{_format_timestamp(index[0])} to {_format_timestamp(index[-1])}"


def _format_timestamp(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")



def _find_epw_file(city: str) -> Path:
    city_map = load_city_manifest()
    matches = city_map[city_map["datacentermap_market"] == city]
    if matches.empty:
        normalized_city = city.lower()
        matches = city_map[
            city_map["datacentermap_market"].astype(str).str.lower() == normalized_city
        ]
    if matches.empty:
        raise ValueError(f"Could not find {city!r} in {CITY_MAP_FILE}.")

    row = matches.iloc[0]
    city_index_prefix = str(row.get("epw_filename_prefix") or "").strip()
    if not city_index_prefix:
        try:
            city_index_prefix = f"{int(float(row['repo_city_index'])):03d}_"
        except Exception as exc:
            raise ValueError(f"Missing EPW filename prefix for {city!r} in {CITY_MAP_FILE}.") from exc
    if not city_index_prefix.endswith("_"):
        city_index_prefix = f"{city_index_prefix}_"
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
            f"Time alignment: {result.time_alignment}",
            f"Simulation window: {result.simulation_start_time} to {result.simulation_end_time}",
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
        start_time=args.start_time,
        time_alignment=args.time_alignment,
        max_carbon_gap_hours=args.max_carbon_gap_hours,
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
    parser.add_argument("--city", help="City name from data/coastal_datacenter_city_manifest.xlsx.")
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
        "--start-time",
        default=None,
        help='Optional simulation start timestamp, for example "2025-01-01 00:00".',
    )
    parser.add_argument(
        "--time-alignment",
        choices=["sst", "latest", "start_time"],
        default=None,
        help=(
            "Input time-axis alignment mode. Defaults to sst for seawater and "
            "latest for air_source. Supplying --start-time uses start_time mode."
        ),
    )
    parser.add_argument(
        "--max-carbon-gap-hours",
        type=int,
        default=6,
        help="Maximum consecutive missing carbon-intensity hours to interpolate after alignment.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for the output CSV. Defaults to results/.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--list-cities", action="store_true", help="Print available city names.")
    args = parser.parse_args()

    main(args)
