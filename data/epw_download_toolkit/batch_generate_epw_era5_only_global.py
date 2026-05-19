#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate Actual Meteorological Year (AMY) EPW files from ERA5 only.

V5 changes: robustly handles CDS outputs returned as ZIP archives, detects text/JSON/HTML error files, removes bad cache files before retrying, and supports both ERA5 time-series NetCDF files with only a time dimension and gridded ERA5 NetCDF files with latitude/longitude dimensions.

Why this script exists
----------------------
The era5epw package uses CAMS solar-radiation time-series for radiation variables.
CAMS Radiation Service is satellite-field-of-view limited, so many cities in the
Americas fail with messages such as:
    "is outside of the satellite field of view"

This script avoids that limitation by using ERA5 global variables only:
- temperature, dew point, pressure, wind, cloud cover
- surface solar radiation downwards (GHI proxy)
- total sky direct solar radiation at surface (direct horizontal proxy)
- precipitation and snow depth where available

Input
-----
A CSV table containing:
- EPW latitude
- EPW longitude
- Country/Area
- City / metro
- optionally EPW filename

The coordinate table generated for this project is:
    target_city_map_epw_coordinates_checked.csv

Notes
-----
1. Radiation in this script comes from ERA5 reanalysis, not CAMS satellite radiation.
2. ERA5 radiation variables are accumulated energy in J m-2; the script converts to Wh m-2 by /3600.
3. Direct normal radiation is estimated from ERA5 direct horizontal radiation and solar zenith angle:
       DNI = direct_horizontal / cos(zenith)
   This is an approximation, but it is globally available and avoids CAMS FOV failures.
4. For high-accuracy solar-energy studies, validate radiation against ground/satellite products
   where available. For data-center cooling simulation, temperature/humidity/wind usually dominate.

Windows example
---------------
conda create -n epw2025 python=3.12 -y
conda activate epw2025
pip install cdsapi xarray netCDF4 h5netcdf pandas numpy pvlib timezonefinder tqdm

python batch_generate_epw_era5_only_global.py ^
    --input target_city_map_epw_coordinates_checked.csv ^
    --year 2025 ^
    --out-dir epw_2025_era5_only ^
    --cache-dir era5_only_cache ^
    --status-csv epw_era5_only_status.csv ^
    --zip-output epw_2025_era5_only.zip ^
    --limit 1 ^
    --overwrite
"""
from __future__ import annotations

import argparse
import calendar
import csv
import math
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    import cdsapi
except ImportError as exc:
    raise SystemExit("Missing dependency: cdsapi. Install with: pip install cdsapi") from exc

try:
    import xarray as xr
except ImportError as exc:
    raise SystemExit("Missing dependency: xarray. Install with: pip install xarray netCDF4 h5netcdf") from exc

try:
    import pvlib
except ImportError as exc:
    raise SystemExit("Missing dependency: pvlib. Install with: pip install pvlib") from exc

try:
    from timezonefinder import TimezoneFinder
    from zoneinfo import ZoneInfo
except Exception:
    TimezoneFinder = None
    ZoneInfo = None


ERA5_VARIABLES = [
    "2m_temperature",
    "2m_dewpoint_temperature",
    "surface_pressure",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "total_cloud_cover",
    "surface_solar_radiation_downwards",
    "total_sky_direct_solar_radiation_at_surface",
    "total_precipitation",
    "snow_depth",
]

# xarray variable names in downloaded ERA5 NetCDF files are usually short names.
VAR_ALIASES: Dict[str, List[str]] = {
    "t2m": ["t2m", "2m_temperature"],
    "d2m": ["d2m", "2m_dewpoint_temperature"],
    "sp": ["sp", "surface_pressure"],
    "u10": ["u10", "10m_u_component_of_wind"],
    "v10": ["v10", "10m_v_component_of_wind"],
    "tcc": ["tcc", "total_cloud_cover"],
    "ssrd": ["ssrd", "surface_solar_radiation_downwards"],
    "fdir": ["fdir", "total_sky_direct_solar_radiation_at_surface"],
    "tp": ["tp", "total_precipitation"],
    "sd": ["sd", "sde", "snow_depth"],
}


def safe_filename(text: str, max_len: int = 120) -> str:
    text = str(text)
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text, flags=re.UNICODE)
    text = text.strip("._-")
    return (text[:max_len] or "city")


def get_first_weekday_of_year(year: int) -> str:
    # Monday=0
    return ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][
        pd.Timestamp(year, 1, 1).dayofweek
    ]


def is_leap_year(year: int) -> bool:
    return calendar.isleap(year)


def standard_timezone_offset_hours(lat: float, lon: float, year: int) -> float:
    """Return standard-time UTC offset. Falls back to longitude/15 if timezonefinder unavailable."""
    if TimezoneFinder is None or ZoneInfo is None:
        return round(lon / 15.0)
    try:
        tf = TimezoneFinder()
        tz_name = tf.timezone_at(lat=lat, lng=lon) or tf.closest_timezone_at(lat=lat, lng=lon)
        if not tz_name:
            return round(lon / 15.0)
        # Use January 15 noon to avoid DST in most Northern Hemisphere locations.
        # For Southern Hemisphere, this may be DST; use both Jan and Jul and choose the smaller absolute offset.
        offsets = []
        for month in (1, 7):
            dt = pd.Timestamp(year=year, month=month, day=15, hour=12).to_pydatetime()
            off = ZoneInfo(tz_name).utcoffset(dt)
            if off is not None:
                offsets.append(off.total_seconds() / 3600.0)
        if offsets:
            # Standard offset is usually the one with smaller absolute daylight-saving addition.
            # For most zones this is min(offsets) in the Northern Hemisphere and max in some Western zones.
            # Choose the offset that appears closest to the longitude-derived nominal zone.
            nominal = lon / 15.0
            return float(min(offsets, key=lambda x: abs(x - nominal)))
    except Exception:
        pass
    return float(round(lon / 15.0))


def calc_rh(temp_c: np.ndarray, dew_c: np.ndarray) -> np.ndarray:
    es = 6.112 * np.exp((17.67 * temp_c) / (temp_c + 243.5))
    esd = 6.112 * np.exp((17.67 * dew_c) / (dew_c + 243.5))
    rh = 100.0 * esd / es
    return np.round(np.clip(rh, 0, 100), 1)


def get_var(ds: xr.Dataset, canonical: str) -> xr.DataArray:
    for name in VAR_ALIASES[canonical]:
        if name in ds.data_vars:
            return ds[name]
    available = ", ".join(ds.data_vars)
    raise KeyError(f"Cannot find ERA5 variable '{canonical}'. Available variables: {available}")


def normalize_dataset_to_point(ds: xr.Dataset, lat: float, lon: float) -> xr.Dataset:
    """
    Return a point dataset for both ERA5 access modes.

    CDS ERA5 time-series output usually has only a time dimension and may expose
    latitude/longitude as scalar coordinates. In that case there is nothing to
    select. ERA5 gridded fallback output has latitude/longitude dimensions; for
    that case select the nearest grid cell.
    """
    # Different CDS datasets may use latitude/longitude or lat/lon.
    lat_name = next((n for n in ("latitude", "lat") if n in ds.coords or n in ds.dims), None)
    lon_name = next((n for n in ("longitude", "lon") if n in ds.coords or n in ds.dims), None)

    if not lat_name or not lon_name:
        return ds

    # Time-series NetCDF: lat/lon are scalar coordinates, not selectable dimensions.
    lat_dims = tuple(getattr(ds[lat_name], "dims", ())) if lat_name in ds else tuple(ds.coords[lat_name].dims)
    lon_dims = tuple(getattr(ds[lon_name], "dims", ())) if lon_name in ds else tuple(ds.coords[lon_name].dims)
    if len(lat_dims) == 0 and len(lon_dims) == 0:
        return ds

    # Regular gridded ERA5: lat/lon are dimensions or 1-D coordinate axes.
    if lat_name in ds.dims and lon_name in ds.dims:
        try:
            return ds.sel({lat_name: lat, lon_name: lon}, method="nearest")
        except Exception:
            lat_vals = np.asarray(ds[lat_name].values, dtype=float)
            lon_vals = np.asarray(ds[lon_name].values, dtype=float)
            lat_idx = int(np.argmin(np.abs(lat_vals - lat)))
            lon_idx = int(np.argmin(np.abs(lon_vals - lon)))
            return ds.isel({lat_name: lat_idx, lon_name: lon_idx})

    # Curvilinear or uncommon coordinates: leave unchanged; squeeze/drop later.
    return ds


def normalize_time_index(ds: xr.Dataset) -> xr.Dataset:
    for time_name in ("valid_time", "time"):
        if time_name in ds.coords or time_name in ds.dims:
            if time_name != "time":
                ds = ds.rename({time_name: "time"})
            return ds
    raise KeyError("No time coordinate found in downloaded ERA5 NetCDF.")




def file_magic(path: Path, n: int = 512) -> bytes:
    try:
        with path.open("rb") as f:
            return f.read(n)
    except Exception:
        return b""


def classify_downloaded_file(path: Path) -> str:
    """Return a coarse file type based on magic bytes."""
    head = file_magic(path, 512)
    if not head:
        return "missing_or_empty"
    if head.startswith(b"\x89HDF"):
        return "netcdf4_hdf5"
    if head.startswith(b"CDF"):
        return "netcdf3"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06") or head.startswith(b"PK\x07\x08"):
        return "zip"
    if head.startswith(b"GRIB"):
        return "grib"
    # CDS/API errors are often JSON, XML/HTML, or plain text.
    try:
        text = head.decode("utf-8", errors="ignore").strip()
        if text.startswith("{"):
            return "json_or_api_error"
        if text.startswith("<"):
            return "html_or_xml_error"
        if text:
            return "text_or_unknown"
    except Exception:
        pass
    return "unknown_binary"


def read_download_preview(path: Path, n: int = 2000) -> str:
    head = file_magic(path, n)
    if not head:
        return ""
    if head.startswith((b"\x89HDF", b"CDF", b"PK", b"GRIB")):
        return ""
    return head.decode("utf-8", errors="replace").replace("\r", " ").replace("\n", " ")[:1000]


def normalize_cds_download_to_netcdf(path: Path) -> None:
    """Ensure path points to a NetCDF file. Extract from ZIP if CDS returned a ZIP archive."""
    kind = classify_downloaded_file(path)
    if kind in {"netcdf4_hdf5", "netcdf3"}:
        return

    if kind == "zip":
        extract_dir = path.parent / (path.stem + "_unzipped")
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(path, "r") as z:
                members = [m for m in z.namelist() if not m.endswith("/")]
                nc_members = [m for m in members if m.lower().endswith((".nc", ".nc4", ".cdf", ".netcdf"))]
                if not nc_members:
                    grib_members = [m for m in members if m.lower().endswith((".grib", ".grb", ".grib2"))]
                    raise RuntimeError(
                        f"CDS returned a ZIP archive but no NetCDF file was found. "
                        f"members={members[:20]}, grib_members={grib_members[:20]}"
                    )
                member = sorted(nc_members, key=len)[0]
                tmp_nc = extract_dir / Path(member).name
                with z.open(member) as src, tmp_nc.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            extracted_kind = classify_downloaded_file(tmp_nc)
            if extracted_kind not in {"netcdf4_hdf5", "netcdf3"}:
                raise RuntimeError(f"Extracted file is not NetCDF: {tmp_nc}, kind={extracted_kind}")
            tmp_replace = path.with_suffix(path.suffix + ".replace")
            if tmp_replace.exists():
                tmp_replace.unlink()
            shutil.copyfile(tmp_nc, tmp_replace)
            tmp_replace.replace(path)
            shutil.rmtree(extract_dir, ignore_errors=True)
            return
        except zipfile.BadZipFile as exc:
            raise RuntimeError(f"CDS output has ZIP magic bytes but cannot be opened as ZIP: {path}: {exc}") from exc

    preview = read_download_preview(path)
    size = path.stat().st_size if path.exists() else 0
    raise RuntimeError(
        f"CDS output is not a readable NetCDF file: {path}; kind={kind}; size={size} bytes; "
        f"preview={preview!r}"
    )

def open_netcdf(path: Path, lat: float, lon: float) -> xr.Dataset:
    normalize_cds_download_to_netcdf(path)
    # Try common engines. h5netcdf often works well on Windows; netcdf4 is the usual default.
    last_exc = None
    for engine in (None, "h5netcdf", "netcdf4"):
        try:
            if engine is None:
                ds = xr.open_dataset(path)
            else:
                ds = xr.open_dataset(path, engine=engine)
            ds = normalize_time_index(ds)
            ds = normalize_dataset_to_point(ds, lat=lat, lon=lon)
            return ds
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"Cannot open NetCDF {path}: {last_exc}")


def download_era5_timeseries(
    client: "cdsapi.Client",
    target_nc: Path,
    year: int,
    lat: float,
    lon: float,
    verbose: bool = False,
) -> None:
    """Download all required variables from ERA5 hourly time-series dataset."""
    target_nc.parent.mkdir(parents=True, exist_ok=True)

    request = {
        "variable": ERA5_VARIABLES,
        "date": [f"{year}-01-01/{year}-12-31"],
        "location": {"latitude": lat, "longitude": lon},
        "data_format": "netcdf",
    }
    client.retrieve("reanalysis-era5-single-levels-timeseries", request, str(target_nc))

    if not target_nc.exists() or target_nc.stat().st_size == 0:
        raise RuntimeError(f"CDS request finished but target NetCDF is missing or empty: {target_nc}")
    normalize_cds_download_to_netcdf(target_nc)


def download_era5_area_fallback_by_month(
    client: "cdsapi.Client",
    target_nc: Path,
    year: int,
    lat: float,
    lon: float,
    tmp_dir: Path,
    verbose: bool = False,
) -> None:
    """Fallback to regular ERA5 single-level data with a small area around the city."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    month_files: List[Path] = []

    for month in range(1, 13):
        month_path = tmp_dir / f"{target_nc.stem}_{year}_{month:02d}.nc"
        if not month_path.exists() or month_path.stat().st_size == 0:
            days = [f"{d:02d}" for d in range(1, calendar.monthrange(year, month)[1] + 1)]
            request = {
                "product_type": "reanalysis",
                "variable": ERA5_VARIABLES,
                "year": [str(year)],
                "month": [f"{month:02d}"],
                "day": days,
                "time": [f"{h:02d}:00" for h in range(24)],
                "data_format": "netcdf",
                "download_format": "unarchived",
                # North, West, South, East. A tiny box ensures one or a few grid cells.
                "area": [lat + 0.10, lon - 0.10, lat - 0.10, lon + 0.10],
            }
            client.retrieve("reanalysis-era5-single-levels", request, str(month_path))
            if not month_path.exists() or month_path.stat().st_size == 0:
                raise RuntimeError(f"Monthly ERA5 fallback output missing or empty: {month_path}")
            normalize_cds_download_to_netcdf(month_path)
        month_files.append(month_path)

    datasets = [open_netcdf(p, lat=lat, lon=lon) for p in month_files]
    ds = xr.concat(datasets, dim="time").sortby("time")
    ds.to_netcdf(target_nc)


def dataset_to_hourly_dataframe(ds: xr.Dataset, lat: float, lon: float, year: int, apply_tz_hours: Optional[float]) -> pd.DataFrame:
    """Convert xarray ERA5 dataset to a DataFrame with one row per hour."""
    ds = normalize_dataset_to_point(ds, lat=lat, lon=lon)
    ds = normalize_time_index(ds)

    # Convert to pandas. If spatial dimensions remain, squeeze them out first.
    ds = ds.squeeze(drop=True)
    data = pd.DataFrame(index=pd.to_datetime(ds["time"].values))
    data.index = data.index.tz_localize(None)

    for canonical in VAR_ALIASES:
        try:
            arr = get_var(ds, canonical).values
            data[canonical] = np.asarray(arr).reshape(-1)
        except KeyError:
            # Snow depth may be missing from some variants; keep NaN.
            if canonical == "sd":
                data[canonical] = np.nan
            else:
                raise

    data = data.sort_index()
    data = data[~data.index.duplicated(keep="first")]

    if apply_tz_hours is not None:
        data.index = data.index + pd.to_timedelta(apply_tz_hours, unit="h")

    start = pd.Timestamp(year=year, month=1, day=1, hour=0)
    end = pd.Timestamp(year=year, month=12, day=31, hour=23)
    data = data.loc[(data.index >= start) & (data.index <= end)].copy()

    # If a requested local-time shift removed first/last hours, reindex and interpolate/fill.
    full_index = pd.date_range(start, end, freq="h")
    data = data.reindex(full_index)
    data = data.interpolate(limit_direction="both").ffill().bfill()

    return data


def make_epw_dataframe(data: pd.DataFrame, lat: float, lon: float, elevation_m: float = 0.0) -> pd.DataFrame:
    times = data.index

    temp_c = data["t2m"].to_numpy(dtype=float) - 273.15
    dew_c = data["d2m"].to_numpy(dtype=float) - 273.15
    press = data["sp"].to_numpy(dtype=float)
    u10 = data["u10"].to_numpy(dtype=float)
    v10 = data["v10"].to_numpy(dtype=float)
    cloud = np.clip(data["tcc"].to_numpy(dtype=float) * 10.0, 0, 10)

    # ERA5 accumulation variables are J/m2. Convert to Wh/m2.
    ghi = np.clip(data["ssrd"].to_numpy(dtype=float) / 3600.0, 0, None)
    direct_horizontal = np.clip(data["fdir"].to_numpy(dtype=float) / 3600.0, 0, None)
    dhi = np.clip(ghi - direct_horizontal, 0, None)

    # Estimate DNI from direct horizontal radiation and solar zenith.
    # Use the center of the EPW hour approximation.
    solpos = pvlib.solarposition.get_solarposition(
        time=pd.DatetimeIndex(times).tz_localize("UTC"),
        latitude=lat,
        longitude=lon,
        altitude=elevation_m,
    )
    cos_zenith = np.cos(np.deg2rad(solpos["zenith"].to_numpy(dtype=float)))
    dni = np.zeros_like(ghi)
    mask = cos_zenith > 0.065
    dni[mask] = direct_horizontal[mask] / cos_zenith[mask]
    dni = np.clip(dni, 0, 1400)

    wind_speed = np.sqrt(u10 ** 2 + v10 ** 2)
    wind_dir = (180 + np.degrees(np.arctan2(u10, v10))) % 360

    total_precip_mm = np.clip(data["tp"].to_numpy(dtype=float) * 1000.0, 0, None)
    snow_depth_cm = data["sd"].to_numpy(dtype=float) * 100.0
    snow_depth_cm = np.where(np.isfinite(snow_depth_cm), snow_depth_cm, 999)

    epw = pd.DataFrame(
        {
            "Year": times.year,
            "Month": times.month,
            "Day": times.day,
            "Hour": times.hour + 1,  # EPW hours are 1-24
            "Minute": 0,
            "Data Source and Uncertainty Flags": "9",
            "Dry Bulb Temperature": np.round(temp_c, 1),
            "Dew Point Temperature": np.round(dew_c, 1),
            "Relative Humidity": calc_rh(temp_c, dew_c),
            "Atmospheric Station Pressure": np.round(press, 0),
            "Extraterrestrial Horizontal Radiation": 9999,
            "Extraterrestrial Direct Normal Radiation": 9999,
            "Horizontal Infrared Radiation Intensity": 9999,
            "Global Horizontal Radiation": np.round(ghi, 1),
            "Direct Normal Radiation": np.round(dni, 1),
            "Diffuse Horizontal Radiation": np.round(dhi, 1),
            "Global Horizontal Illuminance": np.round(110 * ghi, 0),
            "Direct Normal Illuminance": np.round(105 * dni, 0),
            "Diffuse Horizontal Illuminance": np.round(119 * dhi, 0),
            "Zenith Luminance": 9999,
            "Wind Direction": np.round(wind_dir, 0),
            "Wind Speed": np.round(wind_speed, 1),
            "Total Sky Cover": np.round(cloud, 0),
            "Opaque Sky Cover": np.round(cloud, 0),
            "Visibility": 9999,
            "Ceiling Height": 77777,
            "Present Weather Observation": 0,
            "Present Weather Codes": 999999999,
            "Precipitable Water": 999,
            "Aerosol Optical Depth": 999,
            "Snow Depth": np.round(snow_depth_cm, 1),
            "Days Since Last Snowfall": 99,
            "Albedo": 999,
            "Liquid Precipitation Depth": np.round(total_precip_mm, 1),
            "Liquid Precipitation Quantity": np.where(total_precip_mm > 0, 1, 0),
        }
    )
    return epw


def write_epw(path: Path, city_name: str, country: str, lat: float, lon: float, tz_hours: float, elevation_m: float, year: int, epw_df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    ground_temps = "0"
    data_period_end_date = f"{int(epw_df.iloc[-1]['Month'])}/{int(epw_df.iloc[-1]['Day'])}"

    header = [
        f"LOCATION,{city_name},,{country},ERA5 (ECMWF),n/a,{lat:.4f},{lon:.4f},{tz_hours:g},{int(round(elevation_m))}",
        "DESIGN CONDITIONS,0",
        "TYPICAL/EXTREME PERIODS,0",
        f"GROUND TEMPERATURES,{ground_temps}",
        f"HOLIDAYS/DAYLIGHT SAVINGS,{'Yes' if is_leap_year(year) else 'No'},0,0,0",
        "COMMENTS 1,Actual Meteorological Year generated from ERA5 global variables only; no CAMS radiation service used.",
        "COMMENTS 2,Solar radiation from ERA5 ssrd/fdir; DNI estimated from solar zenith.",
        f"DATA PERIODS,1,1,Data,{get_first_weekday_of_year(year)},1/1,{data_period_end_date}",
    ]

    with path.open("w", encoding="utf-8", newline="") as f:
        for line in header:
            f.write(line + os.linesep)
        epw_df.to_csv(f, index=False, header=False, lineterminator=os.linesep)


def validate_epw(path: Path) -> dict:
    result = {
        "actual_epw_path": str(path),
        "hourly_rows": 0,
        "has_feb29": False,
        "drybulb_col7_numeric": True,
        "rh_col9_numeric": True,
        "pressure_col10_numeric": True,
        "drybulb_min_C": None,
        "drybulb_max_C": None,
        "rh_min_pct": None,
        "rh_max_pct": None,
        "pressure_min_Pa": None,
        "pressure_max_Pa": None,
        "validation_status": "failed",
        "validation_notes": "",
    }
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        result["validation_notes"] = f"read_error: {exc}"
        return result

    data_rows = []
    for line in raw:
        if not line or line.startswith((
            "LOCATION", "DESIGN CONDITIONS", "TYPICAL/EXTREME",
            "GROUND TEMPERATURES", "HOLIDAYS/DAYLIGHT",
            "COMMENTS", "DATA PERIODS"
        )):
            continue
        parts = line.split(",")
        if len(parts) >= 10:
            try:
                int(float(parts[0])); int(float(parts[1])); int(float(parts[2])); int(float(parts[3]))
                data_rows.append(parts)
            except Exception:
                continue

    result["hourly_rows"] = len(data_rows)
    drys, rhs, ps = [], [], []
    notes = []
    for parts in data_rows:
        try:
            month = int(float(parts[1])); day = int(float(parts[2]))
            if month == 2 and day == 29:
                result["has_feb29"] = True
        except Exception:
            pass
        try:
            drys.append(float(parts[6]))
        except Exception:
            result["drybulb_col7_numeric"] = False
        try:
            rhs.append(float(parts[8]))
        except Exception:
            result["rh_col9_numeric"] = False
        try:
            ps.append(float(parts[9]))
        except Exception:
            result["pressure_col10_numeric"] = False

    if drys:
        result["drybulb_min_C"] = min(drys)
        result["drybulb_max_C"] = max(drys)
    if rhs:
        result["rh_min_pct"] = min(rhs)
        result["rh_max_pct"] = max(rhs)
    if ps:
        result["pressure_min_Pa"] = min(ps)
        result["pressure_max_Pa"] = max(ps)

    ok = True
    if result["hourly_rows"] != 8760:
        ok = False
        notes.append(f"hourly_rows={result['hourly_rows']} not 8760")
    if result["has_feb29"]:
        ok = False
        notes.append("contains Feb 29")
    for k in ("drybulb_col7_numeric", "rh_col9_numeric", "pressure_col10_numeric"):
        if not result[k]:
            ok = False
            notes.append(f"{k}=False")
    if drys and (min(drys) < -100 or max(drys) > 80):
        notes.append("drybulb outside broad plausibility range [-100, 80] C")
    if rhs and (min(rhs) < 0 or max(rhs) > 110):
        notes.append("RH outside broad plausibility range [0, 110] %")
    if ps and (min(ps) < 30000 or max(ps) > 120000):
        notes.append("pressure outside broad plausibility range [30000, 120000] Pa")

    result["validation_status"] = "ok" if ok else "failed"
    result["validation_notes"] = "; ".join(notes)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="target_city_map_epw_coordinates_checked.csv")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--out-dir", default="epw_2025_era5_only")
    parser.add_argument("--cache-dir", default="era5_only_cache")
    parser.add_argument("--status-csv", default="epw_era5_only_status.csv")
    parser.add_argument("--zip-output", default="epw_2025_era5_only.zip")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--apply-time-zone-to-data", action="store_true",
                        help="Shift timestamps by standard UTC offset. Default is UTC timestamps.")
    parser.add_argument("--fallback-area", action="store_true",
                        help="If ERA5 time-series request fails, fallback to monthly area requests.")
    parser.add_argument("--verbose-cds", action="store_true")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise SystemExit(f"Input table not found: {input_path}")

    rows = pd.read_csv(input_path, encoding="utf-8-sig")
    if args.limit and args.limit > 0:
        rows = rows.head(args.limit).copy()

    lat_col = "EPW latitude" if "EPW latitude" in rows.columns else "City latitude"
    lon_col = "EPW longitude" if "EPW longitude" in rows.columns else "City longitude"
    city_col = "City / metro"
    country_col = "Country/Area"

    out_dir = Path(args.out_dir)
    cache_dir = Path(args.cache_dir)
    tmp_dir = cache_dir / "_monthly_fallback"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    client = cdsapi.Client()

    status_rows = []
    for idx, row in rows.iterrows():
        country = str(row.get(country_col, ""))
        city = str(row.get(city_col, f"city_{idx:03d}"))
        lat = float(row[lat_col])
        lon = float(row[lon_col])
        elevation_m = float(row.get("Elevation_m", 0.0) if pd.notna(row.get("Elevation_m", 0.0)) else 0.0)
        tz_hours = standard_timezone_offset_hours(lat, lon, args.year)

        if "EPW filename" in rows.columns and pd.notna(row["EPW filename"]):
            epw_name = str(row["EPW filename"])
        else:
            epw_name = f"{idx:03d}_{safe_filename(country)}_{safe_filename(city)}_{args.year}_AMY_ERA5.epw"

        epw_path = out_dir / epw_name
        nc_path = cache_dir / (Path(epw_name).stem + ".nc")

        status = row.to_dict()
        status.update({
            "download_status": "",
            "generation_status": "",
            "validation_status": "",
            "actual_epw_path": str(epw_path),
            "era5_cache_file": str(nc_path),
            "time_zone_hours": tz_hours,
            "apply_time_zone_to_data": args.apply_time_zone_to_data,
            "notes": "",
        })

        print(f"[{len(status_rows)+1}/{len(rows)}] {city}, {country} | lat={lat:.5f}, lon={lon:.5f}, tz={tz_hours:g}")

        if epw_path.exists() and not args.overwrite:
            print("    skip existing EPW")
            v = validate_epw(epw_path)
            status.update(v)
            status["download_status"] = "skipped_existing"
            status["generation_status"] = "skipped_existing"
            status_rows.append(status)
            continue

        try:
            last_exc = None
            for attempt in range(1, args.retries + 1):
                try:
                    if not nc_path.exists() or nc_path.stat().st_size == 0 or args.overwrite:
                        if nc_path.exists() and args.overwrite:
                            nc_path.unlink()
                        try:
                            download_era5_timeseries(
                                client=client,
                                target_nc=nc_path,
                                year=args.year,
                                lat=lat,
                                lon=lon,
                                verbose=args.verbose_cds,
                            )
                        except Exception as exc:
                            if not args.fallback_area:
                                raise
                            print(f"    time-series request failed, fallback to monthly area request: {exc}")
                            download_era5_area_fallback_by_month(
                                client=client,
                                target_nc=nc_path,
                                year=args.year,
                                lat=lat,
                                lon=lon,
                                tmp_dir=tmp_dir,
                                verbose=args.verbose_cds,
                            )
                        time.sleep(args.sleep)

                    ds = open_netcdf(nc_path, lat=lat, lon=lon)
                    data = dataset_to_hourly_dataframe(
                        ds=ds,
                        lat=lat,
                        lon=lon,
                        year=args.year,
                        apply_tz_hours=tz_hours if args.apply_time_zone_to_data else None,
                    )
                    epw_df = make_epw_dataframe(data=data, lat=lat, lon=lon, elevation_m=elevation_m)
                    write_epw(
                        path=epw_path,
                        city_name=city,
                        country=country,
                        lat=lat,
                        lon=lon,
                        tz_hours=tz_hours,
                        elevation_m=elevation_m,
                        year=args.year,
                        epw_df=epw_df,
                    )
                    status["download_status"] = "downloaded_or_cached"
                    status["generation_status"] = "generated"
                    break
                except Exception as exc:
                    last_exc = exc
                    print(f"    attempt {attempt} failed: {exc}")
                    # If CDS produced a non-NetCDF or partially written cache file, remove it before retrying.
                    try:
                        if nc_path.exists() and classify_downloaded_file(nc_path) not in {"netcdf4_hdf5", "netcdf3"}:
                            bad_path = nc_path.with_suffix(nc_path.suffix + f".bad_attempt{attempt}")
                            if bad_path.exists():
                                bad_path.unlink()
                            nc_path.replace(bad_path)
                            print(f"    moved bad cache to: {bad_path}")
                    except Exception:
                        try:
                            if nc_path.exists():
                                nc_path.unlink()
                        except Exception:
                            pass
                    time.sleep(min(3 * attempt, 10))
            else:
                raise RuntimeError(last_exc)

            v = validate_epw(epw_path)
            status.update(v)
            if status.get("validation_status") == "ok":
                print("    OK")
            else:
                print(f"    validation failed: {status.get('validation_notes')}")
        except Exception as exc:
            status["download_status"] = "failed"
            status["generation_status"] = "failed"
            status["validation_status"] = "failed"
            status["notes"] = str(exc)
            print(f"    FAILED: {exc}")

        status_rows.append(status)

        # Write incremental status after each city.
        pd.DataFrame(status_rows).to_csv(args.status_csv, index=False, encoding="utf-8-sig")

    status_df = pd.DataFrame(status_rows)
    status_df.to_csv(args.status_csv, index=False, encoding="utf-8-sig")

    zip_path = Path(args.zip_output)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for epw in sorted(out_dir.glob("*.epw")):
            z.write(epw, epw.name)
        z.write(args.status_csv, Path(args.status_csv).name)

    ok_count = int((status_df.get("validation_status", pd.Series(dtype=str)) == "ok").sum())
    print(f"Validated OK: {ok_count}/{len(status_df)}")
    print(f"EPW dir: {out_dir.resolve()}")
    print(f"Status CSV: {Path(args.status_csv).resolve()}")
    print(f"ZIP output: {zip_path.resolve()}")
    return 0 if ok_count == len(status_df) else 2


if __name__ == "__main__":
    raise SystemExit(main())
