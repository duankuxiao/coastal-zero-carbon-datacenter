"""Baseline comparison for toolkit-ready coastal data-center cities.

The script evaluates every city marked toolkit-ready in
data/coastal_datacenter_city_manifest.xlsx with both air-source and seawater-source cooling.
It writes four CSV files:

1. air-source city results
2. seawater-source city results
3. aggregate summary with air-source totals, seawater totals, and savings pct
4. country-level seawater improvement summary vs air-source cooling

Annual offshore wind capacity is sized by annual energy balance only. The
comparison uses the SST timestamp window for both cooling modes by default, so
air-source and seawater-source carbon emissions use the same carbon-intensity
period as the seawater temperatures. Offshore wind inputs are resolved through
data/offshore_wind_download_toolkit/strict_coastal_download_manifest.csv.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from energy.calculate_datacenter_energy import (
    CARBON_INTENSITY_FILE,
    DEFAULT_OUTPUT_DIR,
    SST_FILE,
    WORKLOAD_FILE,
    DataCenterEnergyResult,
    calculate_data_center_energy,
    load_city_manifest,
)
from renewables.calculate_wind_capacity import WindResourceResult, calculate_wind_resource
from utils.output_tables import write_cooling_output_tables
from utils.tools import _resolve_baseline_alignment, _resolve_path


ROOT_DIR = Path(__file__).resolve().parent.parent

def run_baseline(
        workload_file: str | Path = WORKLOAD_FILE,
        rated_it_power_kw: float = 20000.0,
        idle_power_fraction: float = 0.3,
        hours: int | None = 8760,
        start_time: str | None = None,
        time_alignment: str | None = "sst",
        max_carbon_gap_hours: int = 6,
        hub_height_m: float = 150.0,
        wind_loss_fraction: float = 0.15,
        wind_cut_in: float = 3.0,
        wind_rated: float = 12.0,
        wind_cut_out: float = 25.0,
        output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Path]]:
    """Run the coastal baseline and save city, aggregate, and country result tables."""
    output_path = Path(output_dir)
    if not output_path.is_absolute():
        output_path = ROOT_DIR / output_path
    output_path.mkdir(parents=True, exist_ok=True)

    baseline_alignment = _resolve_baseline_alignment(start_time, time_alignment)
    city_map = load_city_manifest()
    city_rows = city_map[["country", "datacentermap_market"]].dropna(subset=["datacentermap_market"]).copy()
    cities = city_rows["datacentermap_market"].astype(str).tolist()

    carbon_df = pd.read_csv(CARBON_INTENSITY_FILE)
    sst_df = pd.read_csv(SST_FILE)

    air_rows: list[dict[str, object]] = []
    seawater_rows: list[dict[str, object]] = []
    skipped: list[tuple[str, str]] = []

    for city_index, city_row in enumerate(city_rows.itertuples(index=False), start=1):
        country = str(city_row.country)
        city = str(city_row.datacentermap_market)
        print(f"Processing {city_index}/{len(cities)}: {city}")
        carbon_ok, carbon_reason = _valid_nonzero_city_series(carbon_df, city, "carbon intensity")
        sst_ok, sst_reason = _valid_nonzero_city_series(sst_df, city, "sea surface temperature")
        if not carbon_ok:
            skipped.append((city, carbon_reason))
            continue
        if not sst_ok:
            skipped.append((city, sst_reason))
            continue

        try:
            wind_resource = calculate_wind_resource(
                city=city,
                hub_height_m=hub_height_m,
                loss_fraction=wind_loss_fraction,
                cut_in=wind_cut_in,
                rated=wind_rated,
                cut_out=wind_cut_out,
            )
            air_result = calculate_data_center_energy(
                city=city,
                cooling_type="air_source",
                workload_file=workload_file,
                rated_it_power_kw=rated_it_power_kw,
                idle_power_fraction=idle_power_fraction,
                hours=hours,
                start_time=start_time,
                time_alignment=baseline_alignment,
                max_carbon_gap_hours=max_carbon_gap_hours,
                progress=False,
            )
            seawater_result = calculate_data_center_energy(
                city=city,
                cooling_type="seawater",
                workload_file=workload_file,
                rated_it_power_kw=rated_it_power_kw,
                idle_power_fraction=idle_power_fraction,
                hours=hours,
                start_time=start_time,
                time_alignment=baseline_alignment,
                max_carbon_gap_hours=max_carbon_gap_hours,
                progress=False,
            )
        except Exception as exc:
            skipped.append((city, str(exc)))
            continue

        air_rows.append(_build_city_result_row(air_result, wind_resource, country=country))
        seawater_rows.append(_build_city_result_row(seawater_result, wind_resource, country=country))

    air_results = pd.DataFrame(air_rows)
    seawater_results = pd.DataFrame(seawater_rows)
    output_files = write_cooling_output_tables(
        pd.concat([air_results, seawater_results], ignore_index=True, sort=False),
        output_path,
        hours=hours,
        country_metric_aggregation="sum",
        default_growth_scenario="baseline",
    )

    included_count = int(air_results["city"].nunique()) if not air_results.empty else 0
    print(f"Toolkit-ready coastal cities found: {len(cities)}")
    print(f"Cities included: {included_count}")
    print(f"Cities skipped: {len(skipped)}")
    for city, reason in skipped:
        print(f"Skipped {city}: {reason}")
    print(json.dumps({key: str(path) for key, path in output_files.items()}, indent=2, ensure_ascii=False))

    return air_results, seawater_results, output_files


def _build_city_result_row(
        energy: DataCenterEnergyResult,
        wind: WindResourceResult,
        country: str,
) -> dict[str, object]:
    total_energy_mwh = energy.total_energy_kwh / 1000.0
    required_wind_capacity_mw = total_energy_mwh / wind.wind_generation_per_mw_mwh
    wind_annual_generation_mwh = required_wind_capacity_mw * wind.wind_generation_per_mw_mwh

    return {
        "country": country,
        "city": energy.city,
        "cooling_type": energy.cooling_type,
        "hours": energy.hours,
        "simulation_start_time": energy.simulation_start_time,
        "simulation_end_time": energy.simulation_end_time,
        "time_alignment": energy.time_alignment,
        "carbon_intensity_start_time": energy.carbon_intensity_start_time,
        "carbon_intensity_end_time": energy.carbon_intensity_end_time,
        "sst_start_time": energy.sst_start_time,
        "sst_end_time": energy.sst_end_time,
        "rated_it_power_kw": energy.rated_it_power_kw,
        "server_energy_kwh": energy.it_energy_kwh,
        "server_carbon_emissions_kgco2": energy.it_carbon_emissions_kgco2,
        "cooling_energy_kwh": energy.cooling_energy_kwh,
        "cooling_carbon_emissions_kgco2": energy.cooling_carbon_emissions_kgco2,
        "total_energy_kwh": energy.total_energy_kwh,
        "total_carbon_emissions_kgco2": energy.carbon_emissions_kgco2,
        "required_wind_capacity_mw": required_wind_capacity_mw,
        "wind_annual_generation_mwh": wind_annual_generation_mwh,
        "wind_generation_per_mw_mwh": wind.wind_generation_per_mw_mwh,
        "wind_mean_net_capacity_factor": wind.mean_net_capacity_factor,
        "wind_point_id": wind.point_id,
        "wind_nc_file": wind.wind_nc_file,
        "wind_start_time": wind.wind_start_time,
        "wind_end_time": wind.wind_end_time,
    }


def _valid_nonzero_city_series(
        data: pd.DataFrame,
        city: str,
        label: str,
) -> tuple[bool, str]:
    if city not in data.columns:
        return False, f"{label} column does not exist"

    values = pd.to_numeric(data[city], errors="coerce")
    valid = values.dropna()
    if valid.empty:
        return False, f"{label} column has no numeric values"
    if np.isclose(valid.to_numpy(dtype=float), 0.0).all():
        return False, f"{label} column is all zero"
    return True, ""




def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run air-source vs seawater-source baseline for strict-coastal cities."
    )
    parser.add_argument(
        "--workload-file",
        default=str(WORKLOAD_FILE),
        help="CSV workload file containing a cpu_load column.",
    )
    parser.add_argument("--rated-it-power-kw", type=float, default=20000.0)
    parser.add_argument("--idle-power-fraction", type=float, default=0.3)
    parser.add_argument("--hours", type=int, default=8760)
    parser.add_argument(
        "--start-time",
        default="2025-01-01 00:00",
        help='Optional shared simulation start timestamp, for example "2025-01-01 00:00".',
    )
    parser.add_argument(
        "--time-alignment",
        choices=["sst", "start_time"],
        default="sst",
        help=(
            "Baseline comparison time-axis mode. Default sst uses the SST "
            "timestamp window for both cooling modes. Supplying --start-time "
            "uses start_time mode."
        ),
    )
    parser.add_argument(
        "--max-carbon-gap-hours",
        type=int,
        default=6,
        help="Maximum consecutive missing carbon-intensity hours to interpolate after alignment.",
    )
    parser.add_argument("--hub-height-m", type=float, default=150.0)
    parser.add_argument("--wind-loss-fraction", type=float, default=0.15)
    parser.add_argument("--wind-cut-in", type=float, default=3.0)
    parser.add_argument("--wind-rated", type=float, default=12.0)
    parser.add_argument("--wind-cut-out", type=float, default=25.0)
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for aggregate output CSV files. Defaults to results/.",
    )
    args = parser.parse_args()

    run_baseline(
        workload_file=_resolve_path(args.workload_file, ROOT_DIR),
        rated_it_power_kw=args.rated_it_power_kw,
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
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
