"""
Convert ERA5 hourly wind inputs into a simple hourly offshore wind power series.

This is a transparent baseline model, not a full engineering design model. It is
intended for scenario analysis before wind farm layout, turbine make/model, wake
losses, and curtailment data are known.

Windows example:
    pip install pandas numpy xarray netcdf4
    python compute_hourly_wind_power_from_era5.py ^
        --input-nc era5_strict_coastal_wind/OW_001_xxx_era5_atmos_2024-01-01_2024-12-31.nc ^
        --output-csv OW_001_generation_2024.csv ^
        --capacity-mw 100 --hub-height-m 150
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd
import xarray as xr

# Common ERA5 short-name aliases after netCDF export.
ALIASES: Dict[str, list[str]] = {
    "u100": ["u100", "100m_u_component_of_wind"],
    "v100": ["v100", "100m_v_component_of_wind"],
    "u10": ["u10", "10m_u_component_of_wind"],
    "v10": ["v10", "10m_v_component_of_wind"],
    "t2m": ["t2m", "2m_temperature"],
    "sp": ["sp", "surface_pressure"],
    "d2m": ["d2m", "2m_dewpoint_temperature"],
}


def get_var(ds: xr.Dataset, name: str) -> xr.DataArray:
    for candidate in ALIASES.get(name, [name]):
        if candidate in ds:
            return ds[candidate]
    raise KeyError(f"Required variable not found: {name}; available={list(ds.data_vars)}")


def to_series(da: xr.DataArray) -> pd.Series:
    # Collapse singleton spatial dimensions by selecting nearest/first cell.
    for dim in list(da.dims):
        if dim.lower() in {"latitude", "lat", "longitude", "lon"}:
            da = da.isel({dim: 0})
    s = da.to_series()
    if isinstance(s.index, pd.MultiIndex):
        # Keep the datetime-like level.
        for level in s.index.names:
            if level and "time" in level.lower():
                s.index = s.index.get_level_values(level)
                break
    s.index = pd.to_datetime(s.index, utc=True)
    return s.sort_index()


def dry_air_density(temperature_k: pd.Series, pressure_pa: pd.Series) -> pd.Series:
    r_d = 287.05  # J kg-1 K-1
    return pressure_pa / (r_d * temperature_k)


def hub_height_wind(v10: pd.Series, v100: pd.Series, hub_height_m: float, default_alpha: float = 0.11) -> pd.Series:
    # Power-law extrapolation using 10 m and 100 m winds; offshore alpha is often low.
    # Clip alpha to avoid numerical artefacts during calm hours or coastal grid issues.
    with np.errstate(divide="ignore", invalid="ignore"):
        alpha = np.log(v100 / v10) / np.log(100.0 / 10.0)
    alpha = alpha.replace([np.inf, -np.inf], np.nan).clip(lower=-0.05, upper=0.40).fillna(default_alpha)
    return v100 * (hub_height_m / 100.0) ** alpha


def generic_offshore_power_fraction(
    wind_speed: pd.Series,
    cut_in: float = 3.0,
    rated: float = 12.0,
    cut_out: float = 25.0,
) -> pd.Series:
    # Simple cubic power curve. Replace with manufacturer-specific power curve when available.
    v = wind_speed.astype(float)
    pf = pd.Series(0.0, index=v.index)
    mid = (v >= cut_in) & (v < rated)
    pf.loc[mid] = ((v.loc[mid] ** 3 - cut_in ** 3) / (rated ** 3 - cut_in ** 3)).clip(0, 1)
    pf.loc[(v >= rated) & (v <= cut_out)] = 1.0
    return pf.clip(0, 1)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--input-nc", required=True, type=Path)
    p.add_argument("--output-csv", required=True, type=Path)
    p.add_argument("--capacity-mw", type=float, default=100.0)
    p.add_argument("--hub-height-m", type=float, default=150.0)
    p.add_argument("--loss-fraction", type=float, default=0.15, help="Wake+electrical+availability+curtailment loss fraction")
    p.add_argument("--cut-in", type=float, default=3.0)
    p.add_argument("--rated", type=float, default=12.0)
    p.add_argument("--cut-out", type=float, default=25.0)
    args = p.parse_args()

    ds = xr.open_dataset(args.input_nc)
    u100 = to_series(get_var(ds, "u100"))
    v100 = to_series(get_var(ds, "v100"))
    u10 = to_series(get_var(ds, "u10"))
    v10 = to_series(get_var(ds, "v10"))
    t2m = to_series(get_var(ds, "t2m"))
    sp = to_series(get_var(ds, "sp"))

    # Align all series to common timestamps.
    df = pd.concat({"u100": u100, "v100": v100, "u10": u10, "v10": v10, "t2m": t2m, "sp": sp}, axis=1).dropna()
    df["wind_speed_100m_ms"] = np.hypot(df["u100"], df["v100"])
    df["wind_speed_10m_ms"] = np.hypot(df["u10"], df["v10"])
    df["wind_speed_hub_ms"] = hub_height_wind(df["wind_speed_10m_ms"], df["wind_speed_100m_ms"], args.hub_height_m)
    df["air_density_kg_m3"] = dry_air_density(df["t2m"], df["sp"])

    # Density correction as a first-order adjustment. Keep power fraction bounded.
    density_factor = (df["air_density_kg_m3"] / 1.225).clip(0.85, 1.15)
    pf = generic_offshore_power_fraction(df["wind_speed_hub_ms"], args.cut_in, args.rated, args.cut_out)
    df["capacity_factor_gross"] = (pf * density_factor).clip(0, 1)
    df["capacity_factor_net"] = (df["capacity_factor_gross"] * (1 - args.loss_fraction)).clip(0, 1)
    df["generation_mw"] = args.capacity_mw * df["capacity_factor_net"]
    df["generation_mwh"] = df["generation_mw"]  # Hourly data; if sub-hourly, multiply by interval hours.

    out = df[[
        "wind_speed_10m_ms",
        "wind_speed_100m_ms",
        "wind_speed_hub_ms",
        "air_density_kg_m3",
        "capacity_factor_gross",
        "capacity_factor_net",
        "generation_mw",
        "generation_mwh",
    ]]
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.output_csv, encoding="utf-8-sig", index_label="timestamp_utc")
    print(f"Saved: {args.output_csv}")
    print(f"Annual generation: {out['generation_mwh'].sum()/1e3:.3f} GWh")
    print(f"Mean net capacity factor: {out['capacity_factor_net'].mean():.3f}")


if __name__ == "__main__":
    main()
