"""Estimate offshore wind capacity needed to cover annual data-center energy.

This module combines the existing data-center annual energy calculator with the
existing offshore wind power-curve helpers. It performs an annual energy balance
only: required wind capacity is sized so annual wind generation is at least the
data center's annual IT + cooling energy. It does not model hourly supply-demand
matching, storage, curtailment, or grid constraints.

Example:
    python -m renewables.calculate_wind_capacity --city Shanghai --cooling seawater --rated-it-power-kw 20000 --hours 8760 --json
"""

from __future__ import annotations

import argparse
import json
import math
import re
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from energy.calculate_datacenter_energy import (
    DEFAULT_OUTPUT_DIR,
    WORKLOAD_FILE,
    calculate_data_center_energy,
)


ROOT_DIR = Path(__file__).resolve().parent.parent
OFFSHORE_WIND_DIR = ROOT_DIR / "data" / "offshore_wind_download_toolkit"
WIND_MANIFEST_FILE = OFFSHORE_WIND_DIR / "strict_coastal_offshore_wind_points_manifest.csv"

CoolingType = Literal["air_source", "seawater"]


@dataclass(frozen=True)
class WindCapacityResult:
    city: str
    point_id: str
    wind_nc_file: str
    cooling_type: str
    hours: int
    rated_it_power_kw: float
    datacenter_total_energy_kwh: float
    datacenter_total_energy_mwh: float
    wind_generation_per_mw_mwh: float
    required_wind_capacity_mw: float
    annual_generation_mwh: float
    mean_net_capacity_factor: float
    hub_height_m: float
    loss_fraction: float
    cut_in_ms: float
    rated_wind_speed_ms: float
    cut_out_ms: float
    wind_start_time: str | None
    wind_end_time: str | None


@dataclass(frozen=True)
class WindResourceResult:
    city: str
    point_id: str
    wind_nc_file: str
    wind_generation_per_mw_mwh: float
    mean_net_capacity_factor: float
    wind_start_time: str | None
    wind_end_time: str | None


def calculate_required_wind_capacity(
    city: str,
    cooling_type: CoolingType = "seawater",
    workload_file: str | Path = WORKLOAD_FILE,
    rated_it_power_kw: float = 20000.0,
    idle_power_fraction: float = 0.35,
    hours: int | None = 8760,
    start_time: str | None = None,
    time_alignment: Literal["sst", "latest", "start_time"] | None = None,
    max_carbon_gap_hours: int = 6,
    point_id: str | None = None,
    wind_nc_file: str | Path | None = None,
    hub_height_m: float = 150.0,
    loss_fraction: float = 0.15,
    cut_in: float = 3.0,
    rated: float = 12.0,
    cut_out: float = 25.0,
    progress: bool = True,
) -> WindCapacityResult:
    """Return wind capacity whose annual generation covers data-center energy."""
    datacenter_result = calculate_data_center_energy(
        city=city,
        cooling_type=cooling_type,
        workload_file=workload_file,
        rated_it_power_kw=rated_it_power_kw,
        idle_power_fraction=idle_power_fraction,
        hours=hours,
        start_time=start_time,
        time_alignment=time_alignment,
        max_carbon_gap_hours=max_carbon_gap_hours,
        progress=progress,
    )

    wind_resource = calculate_wind_resource(
        city=city,
        point_id=point_id,
        wind_nc_file=wind_nc_file,
        hub_height_m=hub_height_m,
        loss_fraction=loss_fraction,
        cut_in=cut_in,
        rated=rated,
        cut_out=cut_out,
    )
    generation_per_mw_mwh = wind_resource.wind_generation_per_mw_mwh
    if generation_per_mw_mwh <= 0 or not math.isfinite(generation_per_mw_mwh):
        raise ValueError(
            f"Wind file {wind_resource.wind_nc_file} produced non-positive annual generation "
            "for 1 MW of installed capacity."
        )

    datacenter_total_mwh = datacenter_result.total_energy_kwh / 1000.0
    required_capacity_mw = datacenter_total_mwh / generation_per_mw_mwh

    return WindCapacityResult(
        city=datacenter_result.city,
        point_id=wind_resource.point_id,
        wind_nc_file=wind_resource.wind_nc_file,
        cooling_type=datacenter_result.cooling_type,
        hours=datacenter_result.hours,
        rated_it_power_kw=float(rated_it_power_kw),
        datacenter_total_energy_kwh=float(datacenter_result.total_energy_kwh),
        datacenter_total_energy_mwh=float(datacenter_total_mwh),
        wind_generation_per_mw_mwh=generation_per_mw_mwh,
        required_wind_capacity_mw=float(required_capacity_mw),
        annual_generation_mwh=float(required_capacity_mw * generation_per_mw_mwh),
        mean_net_capacity_factor=wind_resource.mean_net_capacity_factor,
        hub_height_m=float(hub_height_m),
        loss_fraction=float(loss_fraction),
        cut_in_ms=float(cut_in),
        rated_wind_speed_ms=float(rated),
        cut_out_ms=float(cut_out),
        wind_start_time=wind_resource.wind_start_time,
        wind_end_time=wind_resource.wind_end_time,
    )


def calculate_wind_resource(
    city: str,
    point_id: str | None = None,
    wind_nc_file: str | Path | None = None,
    hub_height_m: float = 150.0,
    loss_fraction: float = 0.15,
    cut_in: float = 3.0,
    rated: float = 12.0,
    cut_out: float = 25.0,
) -> WindResourceResult:
    """Return annual generation for 1 MW of wind capacity at a city's wind point."""
    resolved_point_id, resolved_wind_file = _resolve_wind_input(city, point_id, wind_nc_file)
    wind_profile = calculate_wind_generation_profile(
        input_nc=resolved_wind_file,
        capacity_mw=1.0,
        hub_height_m=hub_height_m,
        loss_fraction=loss_fraction,
        cut_in=cut_in,
        rated=rated,
        cut_out=cut_out,
    )
    if wind_profile.empty:
        raise ValueError(f"Wind file {resolved_wind_file} produced an empty generation profile.")

    return WindResourceResult(
        city=city,
        point_id=resolved_point_id,
        wind_nc_file=str(resolved_wind_file),
        wind_generation_per_mw_mwh=float(wind_profile["generation_mwh"].sum()),
        mean_net_capacity_factor=float(wind_profile["capacity_factor_net"].mean()),
        wind_start_time=_format_timestamp(wind_profile.index[0]),
        wind_end_time=_format_timestamp(wind_profile.index[-1]),
    )


def calculate_wind_generation_profile(
    input_nc: str | Path,
    capacity_mw: float = 1.0,
    hub_height_m: float = 150.0,
    loss_fraction: float = 0.15,
    cut_in: float = 3.0,
    rated: float = 12.0,
    cut_out: float = 25.0,
) -> pd.DataFrame:
    """Calculate hourly offshore wind generation from one ERA5 netCDF file."""
    try:
        from renewables.wind_power import (
            dry_air_density,
            generic_offshore_power_fraction,
            hub_height_wind,
        )
    except ModuleNotFoundError as exc:
        missing = exc.name or "xarray/netCDF4"
        raise ModuleNotFoundError(
            "Offshore wind generation requires netCDF4. Install it with: pip install netCDF4"
        ) from exc

    if capacity_mw <= 0:
        raise ValueError("capacity_mw must be positive.")
    if not 0 <= loss_fraction < 1:
        raise ValueError("loss_fraction must be in the range [0, 1).")

    input_path = Path(input_nc)
    if not input_path.exists():
        raise FileNotFoundError(f"Wind input file does not exist: {input_path}")

    wind_inputs = _read_era5_wind_inputs(input_path)
    u100 = wind_inputs["u100"]
    v100 = wind_inputs["v100"]
    u10 = wind_inputs["u10"]
    v10 = wind_inputs["v10"]
    t2m = wind_inputs["t2m"]
    sp = wind_inputs["sp"]

    df = pd.concat(
        {"u100": u100, "v100": v100, "u10": u10, "v10": v10, "t2m": t2m, "sp": sp},
        axis=1,
    ).dropna()
    if df.empty:
        raise ValueError(f"Wind input file {input_path} has no complete hourly rows.")

    df["wind_speed_100m_ms"] = np.hypot(df["u100"], df["v100"])
    df["wind_speed_10m_ms"] = np.hypot(df["u10"], df["v10"])
    df["wind_speed_hub_ms"] = hub_height_wind(
        df["wind_speed_10m_ms"],
        df["wind_speed_100m_ms"],
        hub_height_m,
    )
    df["air_density_kg_m3"] = dry_air_density(df["t2m"], df["sp"])
    density_factor = (df["air_density_kg_m3"] / 1.225).clip(0.85, 1.15)
    power_fraction = generic_offshore_power_fraction(
        df["wind_speed_hub_ms"],
        cut_in=cut_in,
        rated=rated,
        cut_out=cut_out,
    )
    df["capacity_factor_gross"] = (power_fraction * density_factor).clip(0, 1)
    df["capacity_factor_net"] = (df["capacity_factor_gross"] * (1 - loss_fraction)).clip(0, 1)
    df["generation_mw"] = float(capacity_mw) * df["capacity_factor_net"]
    df["generation_mwh"] = df["generation_mw"]

    return df[
        [
            "wind_speed_10m_ms",
            "wind_speed_100m_ms",
            "wind_speed_hub_ms",
            "air_density_kg_m3",
            "capacity_factor_gross",
            "capacity_factor_net",
            "generation_mw",
            "generation_mwh",
        ]
    ]


def _read_era5_wind_inputs(input_path: Path) -> dict[str, pd.Series]:
    try:
        from netCDF4 import Dataset, num2date
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "Offshore wind generation requires netCDF4. Install it with: pip install netCDF4"
        ) from exc

    if zipfile.is_zipfile(input_path):
        with zipfile.ZipFile(input_path) as archive:
            nc_names = [name for name in archive.namelist() if name.lower().endswith(".nc")]
            if not nc_names:
                raise ValueError(f"ZIP file {input_path} does not contain a .nc file.")
            payload = archive.read(nc_names[0])
        dataset = Dataset("inmemory.nc", memory=payload)
    else:
        dataset = Dataset(str(input_path))

    try:
        time_index = _read_netcdf_time_index(dataset, num2date)
        return {
            "u100": _netcdf_var_to_series(dataset, "u100", time_index),
            "v100": _netcdf_var_to_series(dataset, "v100", time_index),
            "u10": _netcdf_var_to_series(dataset, "u10", time_index),
            "v10": _netcdf_var_to_series(dataset, "v10", time_index),
            "t2m": _netcdf_var_to_series(dataset, "t2m", time_index),
            "sp": _netcdf_var_to_series(dataset, "sp", time_index),
        }
    finally:
        dataset.close()


def _read_netcdf_time_index(dataset: object, num2date_func) -> pd.DatetimeIndex:
    time_var = None
    for candidate in ("valid_time", "time"):
        if candidate in dataset.variables:
            time_var = dataset.variables[candidate]
            break
    if time_var is None:
        raise KeyError(f"No time variable found. Available variables: {list(dataset.variables)}")

    values = np.asarray(time_var[:])
    units = getattr(time_var, "units", None)
    calendar = getattr(time_var, "calendar", "standard")
    if units:
        decoded = num2date_func(values, units=units, calendar=calendar, only_use_cftime_datetimes=False)
        timestamps = pd.to_datetime([item.isoformat() for item in decoded], utc=True)
    else:
        timestamps = pd.to_datetime(values, unit="s", utc=True)
    return pd.DatetimeIndex(timestamps).tz_convert(None)


def _netcdf_var_to_series(dataset: object, name: str, time_index: pd.DatetimeIndex) -> pd.Series:
    aliases = {
        "u100": ["u100", "100m_u_component_of_wind"],
        "v100": ["v100", "100m_v_component_of_wind"],
        "u10": ["u10", "10m_u_component_of_wind"],
        "v10": ["v10", "10m_v_component_of_wind"],
        "t2m": ["t2m", "2m_temperature"],
        "sp": ["sp", "surface_pressure"],
    }
    variable = None
    for candidate in aliases.get(name, [name]):
        if candidate in dataset.variables:
            variable = dataset.variables[candidate]
            break
    if variable is None:
        raise KeyError(f"Required variable not found: {name}; available={list(dataset.variables)}")

    data = np.asarray(variable[:], dtype=float)
    data = np.squeeze(data)
    if data.ndim == 0:
        values = np.full(len(time_index), float(data))
    elif data.shape[0] == len(time_index):
        values = data.reshape((len(time_index), -1))[:, 0]
    elif data.shape[-1] == len(time_index):
        values = np.moveaxis(data, -1, 0).reshape((len(time_index), -1))[:, 0]
    else:
        raise ValueError(
            f"Variable {name!r} shape {data.shape} cannot be aligned to {len(time_index)} timestamps."
        )
    return pd.Series(values, index=time_index, dtype="float64").sort_index()


def save_result_csv(
    result: WindCapacityResult,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    filename = (
        f"wind_capacity_{_filename_token(result.city)}_"
        f"{result.cooling_type}_{_format_power_token(result.rated_it_power_kw)}.csv"
    )
    csv_path = output_path / filename
    pd.DataFrame([asdict(result)]).to_csv(csv_path, index=False, encoding="utf-8-sig")
    return csv_path


def _resolve_wind_input(
    city: str,
    point_id: str | None,
    wind_nc_file: str | Path | None,
) -> tuple[str, Path]:
    if wind_nc_file is not None:
        wind_path = Path(wind_nc_file)
        if not wind_path.is_absolute():
            wind_path = ROOT_DIR / wind_path
        return point_id or _point_id_from_filename(wind_path) or "custom", wind_path

    manifest = pd.read_csv(WIND_MANIFEST_FILE)
    if point_id:
        matches = manifest[manifest["point_id"].astype(str).str.lower() == point_id.lower()]
    else:
        normalized_city = _normalize_name(city)
        matches = manifest[
            manifest["city_metro"].astype(str).map(_normalize_name) == normalized_city
        ]
        if matches.empty:
            matches = manifest[
                manifest["city_metro"].astype(str).map(_normalize_name).str.contains(
                    normalized_city,
                    regex=False,
                )
            ]
    if matches.empty:
        raise ValueError(
            f"Could not find offshore wind input metadata for city={city!r}, point_id={point_id!r} "
            f"in {WIND_MANIFEST_FILE}."
        )
    if len(matches) > 1 and not point_id:
        candidates = ", ".join(matches["city_metro"].astype(str).head(10))
        raise ValueError(
            f"City {city!r} matched multiple offshore wind points: {candidates}. "
            "Use --point-id to choose one."
        )

    row = matches.iloc[0]
    resolved_point_id = str(row["point_id"])
    matching_files = sorted(OFFSHORE_WIND_DIR.glob(f"{resolved_point_id}_*.nc"))
    if not matching_files:
        raise FileNotFoundError(
            f"Could not find ERA5 netCDF file for {resolved_point_id} in {OFFSHORE_WIND_DIR}."
        )
    return resolved_point_id, matching_files[0]


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def _point_id_from_filename(path: Path) -> str | None:
    match = re.match(r"(OW_\d+)", path.name, flags=re.IGNORECASE)
    return match.group(1).upper() if match else None


def _filename_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", value.strip())
    return token.strip("_") or "unknown"


def _format_power_token(rated_it_power_kw: float) -> str:
    if float(rated_it_power_kw).is_integer():
        return f"{int(rated_it_power_kw)}kW"
    return f"{rated_it_power_kw:g}kW".replace(".", "p")


def _format_timestamp(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _format_result(result: WindCapacityResult) -> str:
    return "\n".join(
        [
            f"City: {result.city}",
            f"Offshore wind point: {result.point_id}",
            f"Cooling type: {result.cooling_type}",
            f"Data-center annual energy: {result.datacenter_total_energy_mwh:,.2f} MWh",
            f"Wind generation per MW: {result.wind_generation_per_mw_mwh:,.2f} MWh/MW-year",
            f"Required wind capacity: {result.required_wind_capacity_mw:,.2f} MW",
            f"Mean net capacity factor: {result.mean_net_capacity_factor:.3f}",
            f"Wind window: {result.wind_start_time} to {result.wind_end_time}",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Estimate offshore wind capacity required to cover annual data-center energy."
    )
    parser.add_argument("--city", required=True, help="City name used by the data-center model.")
    parser.add_argument(
        "--cooling",
        default="seawater",
        choices=["air_source", "seawater", "conventional", "ashp", "swhp"],
        help="Cooling system type passed to calculate_datacenter_energy.",
    )
    parser.add_argument("--workload-file", default=str(WORKLOAD_FILE))
    parser.add_argument("--rated-it-power-kw", type=float, default=20000.0)
    parser.add_argument("--idle-power-fraction", type=float, default=0.35)
    parser.add_argument("--hours", type=int, default=8760)
    parser.add_argument("--start-time", default=None)
    parser.add_argument(
        "--time-alignment",
        choices=["sst", "latest", "start_time"],
        default=None,
    )
    parser.add_argument("--max-carbon-gap-hours", type=int, default=6)
    parser.add_argument("--point-id", default=None, help="Offshore wind point_id such as OW_006.")
    parser.add_argument("--wind-nc-file", default=None, help="Optional direct path to an ERA5 wind netCDF file.")
    parser.add_argument("--hub-height-m", type=float, default=150.0)
    parser.add_argument("--loss-fraction", type=float, default=0.15)
    parser.add_argument("--cut-in", type=float, default=3.0)
    parser.add_argument("--rated", type=float, default=12.0)
    parser.add_argument("--cut-out", type=float, default=25.0)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--no-save", action="store_true", help="Do not write a result CSV.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--quiet", action="store_true", help="Suppress data-center progress messages.")
    args = parser.parse_args()

    result = calculate_required_wind_capacity(
        city=args.city,
        cooling_type=args.cooling,
        workload_file=args.workload_file,
        rated_it_power_kw=args.rated_it_power_kw,
        idle_power_fraction=args.idle_power_fraction,
        hours=args.hours,
        start_time=args.start_time,
        time_alignment=args.time_alignment,
        max_carbon_gap_hours=args.max_carbon_gap_hours,
        point_id=args.point_id,
        wind_nc_file=args.wind_nc_file,
        hub_height_m=args.hub_height_m,
        loss_fraction=args.loss_fraction,
        cut_in=args.cut_in,
        rated=args.rated,
        cut_out=args.cut_out,
        progress=not args.quiet,
    )
    csv_path = None if args.no_save else save_result_csv(result, args.output_dir)

    if args.json:
        payload = asdict(result)
        payload["csv_file"] = None if csv_path is None else str(csv_path)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(_format_result(result))
        if csv_path is not None:
            print(f"CSV saved to: {csv_path}")


if __name__ == "__main__":
    main()
