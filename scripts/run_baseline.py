"""Baseline comparison for toolkit-ready coastal data-center cities.

The script evaluates every city marked toolkit-ready in
data/coastal_datacenter_city_manifest.xlsx with both air-source and seawater-source cooling.
It writes three CSV files:

1. air-source city results
2. seawater-source city results
3. aggregate summary with air-source totals, seawater totals, and savings pct

Annual offshore wind capacity is sized by annual energy balance only. The
comparison uses the SST timestamp window for both cooling modes by default, so
air-source and seawater-source carbon emissions use the same carbon-intensity
period as the seawater temperatures. Offshore wind inputs are resolved through
data/offshore_wind_download_toolkit/strict_coastal_download_manifest.csv.
"""

from __future__ import annotations

import argparse
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

ROOT_DIR = Path(__file__).resolve().parent

RESULT_METRICS = [
    "server_energy_kwh",
    "server_carbon_emissions_kgco2",
    "cooling_energy_kwh",
    "cooling_carbon_emissions_kgco2",
    "total_energy_kwh",
    "total_carbon_emissions_kgco2",
    "required_wind_capacity_mw",
    "wind_annual_generation_mwh",
]


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
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Run the strict-coastal baseline and save three result tables."""
    output_path = Path(output_dir)
    if not output_path.is_absolute():
        output_path = ROOT_DIR / output_path
    output_path.mkdir(parents=True, exist_ok=True)

    baseline_alignment = _resolve_baseline_alignment(start_time, time_alignment)
    city_map = load_city_manifest()
    cities = city_map["datacentermap_market"].dropna().astype(str).tolist()

    carbon_df = pd.read_csv(CARBON_INTENSITY_FILE)
    sst_df = pd.read_csv(SST_FILE)

    air_rows: list[dict[str, object]] = []
    seawater_rows: list[dict[str, object]] = []
    skipped: list[tuple[str, str]] = []

    for city_index, city in enumerate(cities, start=1):
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

        air_rows.append(_build_city_result_row(air_result, wind_resource))
        seawater_rows.append(_build_city_result_row(seawater_result, wind_resource))

    air_results = pd.DataFrame(air_rows)
    seawater_results = pd.DataFrame(seawater_rows)
    summary = _build_summary_table(air_results, seawater_results, rated_it_power_kw, hours)

    suffix = _output_suffix(rated_it_power_kw, hours)
    air_file = output_path / f"baseline_air_source_results_{suffix}.csv"
    seawater_file = output_path / f"baseline_seawater_results_{suffix}.csv"
    summary_file = output_path / f"baseline_summary_{suffix}.csv"
    air_results.to_csv(air_file, index=False, encoding="utf-8-sig")
    seawater_results.to_csv(seawater_file, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_file, index=False, encoding="utf-8-sig")

    included_count = int(air_results["city"].nunique()) if not air_results.empty else 0
    print(f"Strict coastal cities found: {len(cities)}")
    print(f"Cities included: {included_count}")
    print(f"Cities skipped: {len(skipped)}")
    for city, reason in skipped:
        print(f"Skipped {city}: {reason}")
    print(f"Air-source results CSV: {air_file}")
    print(f"Seawater results CSV: {seawater_file}")
    print(f"Summary CSV: {summary_file}")

    return air_results, seawater_results, summary


def _build_city_result_row(
        energy: DataCenterEnergyResult,
        wind: WindResourceResult,
) -> dict[str, object]:
    total_energy_mwh = energy.total_energy_kwh / 1000.0
    required_wind_capacity_mw = total_energy_mwh / wind.wind_generation_per_mw_mwh
    wind_annual_generation_mwh = required_wind_capacity_mw * wind.wind_generation_per_mw_mwh

    return {
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


def _build_summary_table(
        air_results: pd.DataFrame,
        seawater_results: pd.DataFrame,
        rated_it_power_kw: float,
        hours: int | None,
) -> pd.DataFrame:
    rows = [
        _aggregate_result_rows(
            label="air_source_all_regions",
            value_type="absolute",
            results=air_results,
            rated_it_power_kw=rated_it_power_kw,
            hours=hours,
        ),
        _aggregate_result_rows(
            label="seawater_all_regions",
            value_type="absolute",
            results=seawater_results,
            rated_it_power_kw=rated_it_power_kw,
            hours=hours,
        ),
    ]
    rows.append(
        _build_savings_pct_row(
            air_row=rows[0],
            seawater_row=rows[1],
            rated_it_power_kw=rated_it_power_kw,
            hours=hours,
        )
    )
    return pd.DataFrame(rows)


def _aggregate_result_rows(
        label: str,
        value_type: str,
        results: pd.DataFrame,
        rated_it_power_kw: float,
        hours: int | None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "scope": label,
        "value_type": value_type,
        "included_city_count": int(results["city"].nunique()) if not results.empty else 0,
        "hours_per_city": "all_available" if hours is None else hours,
        "rated_it_power_kw_per_city": rated_it_power_kw,
    }
    for metric in RESULT_METRICS:
        row[metric] = float(results[metric].sum()) if metric in results and not results.empty else 0.0
    return row


def _build_savings_pct_row(
        air_row: dict[str, object],
        seawater_row: dict[str, object],
        rated_it_power_kw: float,
        hours: int | None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "scope": "seawater_savings_pct_vs_air_source",
        "value_type": "percent",
        "included_city_count": min(
            int(air_row.get("included_city_count", 0)),
            int(seawater_row.get("included_city_count", 0)),
        ),
        "hours_per_city": "all_available" if hours is None else hours,
        "rated_it_power_kw_per_city": rated_it_power_kw,
    }
    for metric in RESULT_METRICS:
        row[metric] = _pct(float(air_row.get(metric, 0.0)) - float(seawater_row.get(metric, 0.0)), air_row.get(metric, 0.0))
    return row


def _resolve_baseline_alignment(start_time: str | None, time_alignment: str | None) -> str:
    if start_time:
        return "start_time"
    if time_alignment in (None, "sst"):
        return "sst"
    raise ValueError(
        "run_baseline compares air-source and seawater cooling on the SST time window. "
        "Use --time-alignment sst, or provide --start-time for a custom shared window."
    )


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


def _pct(numerator: float, denominator: object) -> float:
    denominator_float = float(denominator)
    if math.isclose(denominator_float, 0.0):
        return math.nan
    return numerator / denominator_float * 100.0


def _output_suffix(rated_it_power_kw: float, hours: int | None) -> str:
    power_token = _format_power_token(rated_it_power_kw)
    hours_token = "all_hours" if hours is None else f"{hours}h"
    return f"{power_token}_{hours_token}"


def _format_power_token(rated_it_power_kw: float) -> str:
    if float(rated_it_power_kw).is_integer():
        return f"{int(rated_it_power_kw)}kW"
    return f"{rated_it_power_kw:g}kW".replace(".", "p")


def _resolve_path(path: str) -> Path:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = ROOT_DIR / resolved
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run air-source vs seawater-source baseline for strict-coastal cities."
    )
    parser.add_argument(
        "--workload-file",
        default=str(WORKLOAD_FILE),
        help="CSV workload file containing a cpu_load column.",
    )
    parser.add_argument("--rated-it-power-kw", type=float, default=1000.0)
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
        workload_file=_resolve_path(args.workload_file),
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
