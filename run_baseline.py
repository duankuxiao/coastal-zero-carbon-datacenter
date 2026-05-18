"""Baseline comparison for strict-coastal data-center cities.

The script evaluates every city marked as "Strict coastal" in
data/target_city_map.csv with both air-source and seawater-source cooling.
It writes only two aggregate CSV files:

1. all city/mode calculation results
2. global seawater-vs-air-source savings summary
"""

from __future__ import annotations

import argparse
import math
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from core.calculate_datacenter_energy import (
    CARBON_INTENSITY_FILE,
    CITY_MAP_FILE,
    DEFAULT_OUTPUT_DIR,
    SST_FILE,
    WORKLOAD_FILE,
    calculate_data_center_energy,
)


ROOT_DIR = Path(__file__).resolve().parent


def run_baseline(
    workload_file: str | Path = WORKLOAD_FILE,
    rated_it_power_kw: float = 20000.0,
    idle_power_fraction: float = 0.3,
    hours: int | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run the strict-coastal baseline and save aggregate result tables."""
    output_path = Path(output_dir)
    if not output_path.is_absolute():
        output_path = ROOT_DIR / output_path
    output_path.mkdir(parents=True, exist_ok=True)

    city_map = pd.read_csv(CITY_MAP_FILE)
    strict_coastal = city_map[
        city_map["Coastal class"].astype(str).str.strip().str.lower() == "strict coastal"
    ]
    cities = strict_coastal["City / metro"].dropna().astype(str).tolist()

    carbon_df = pd.read_csv(CARBON_INTENSITY_FILE)
    sst_df = pd.read_csv(SST_FILE)

    all_rows: list[dict[str, object]] = []
    skipped: list[tuple[str, str]] = []

    for city in cities:
        print(f"Processing {cities.index(city)}/{len(cities)}: {city}")
        carbon_ok, carbon_reason = _valid_nonzero_city_series(carbon_df, city, "carbon intensity")
        sst_ok, sst_reason = _valid_nonzero_city_series(sst_df, city, "sea surface temperature")
        if not carbon_ok:
            skipped.append((city, carbon_reason))
            continue
        if not sst_ok:
            skipped.append((city, sst_reason))
            continue

        try:
            air_result = calculate_data_center_energy(
                city=city,
                cooling_type="air_source",
                workload_file=workload_file,
                rated_it_power_kw=rated_it_power_kw,
                idle_power_fraction=idle_power_fraction,
                hours=hours,
            )
            seawater_result = calculate_data_center_energy(
                city=city,
                cooling_type="seawater",
                workload_file=workload_file,
                rated_it_power_kw=rated_it_power_kw,
                idle_power_fraction=idle_power_fraction,
                hours=hours,
            )
        except Exception as exc:
            skipped.append((city, str(exc)))
            continue

        air_row = asdict(air_result)
        seawater_row = asdict(seawater_result)
        all_rows.extend([air_row, seawater_row])

    all_results = pd.DataFrame(all_rows)
    global_savings = _build_global_savings_table(all_results, rated_it_power_kw, hours)

    suffix = _output_suffix(rated_it_power_kw, hours)
    all_results_file = output_path / f"baseline_strict_coastal_all_results_{suffix}.csv"
    savings_file = output_path / f"baseline_strict_coastal_global_savings_{suffix}.csv"
    all_results.to_csv(all_results_file, index=False, encoding="utf-8-sig")
    global_savings.to_csv(savings_file, index=False, encoding="utf-8-sig")

    print(f"Strict coastal cities found: {len(cities)}")
    print(f"Cities included: {all_results['city'].nunique() if not all_results.empty else 0}")
    print(f"Cities skipped: {len(skipped)}")
    for city, reason in skipped:
        print(f"Skipped {city}: {reason}")
    print(f"All results CSV: {all_results_file}")
    print(f"Global savings CSV: {savings_file}")

    return all_results, global_savings


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


def _build_global_savings_table(
    all_results: pd.DataFrame,
    rated_it_power_kw: float,
    hours: int | None,
) -> pd.DataFrame:
    if all_results.empty:
        return pd.DataFrame()

    air = all_results[all_results["cooling_type"] == "air_source"]
    seawater = all_results[all_results["cooling_type"] == "seawater"]

    air_cooling = float(air["cooling_energy_kwh"].sum())
    seawater_cooling = float(seawater["cooling_energy_kwh"].sum())
    air_total = float(air["total_energy_kwh"].sum())
    seawater_total = float(seawater["total_energy_kwh"].sum())
    air_carbon = float(air["carbon_emissions_kgco2"].sum())
    seawater_carbon = float(seawater["carbon_emissions_kgco2"].sum())

    cooling_savings = air_cooling - seawater_cooling
    total_savings = air_total - seawater_total
    carbon_savings = air_carbon - seawater_carbon

    return pd.DataFrame(
        [
            {
                "scope": "all_included_strict_coastal_cities",
                "included_city_count": int(all_results["city"].nunique()),
                "hours_per_city": "all_available" if hours is None else hours,
                "rated_it_power_kw_per_city": rated_it_power_kw,
                "air_source_cooling_energy_kwh": air_cooling,
                "seawater_cooling_energy_kwh": seawater_cooling,
                "cooling_energy_savings_kwh": cooling_savings,
                "cooling_energy_savings_pct_vs_air_source": _pct(cooling_savings, air_cooling),
                "air_source_total_energy_kwh": air_total,
                "seawater_total_energy_kwh": seawater_total,
                "total_energy_savings_kwh": total_savings,
                "total_energy_savings_pct_vs_air_source": _pct(total_savings, air_total),
                "air_source_carbon_emissions_kgco2": air_carbon,
                "seawater_carbon_emissions_kgco2": seawater_carbon,
                "carbon_emissions_savings_kgco2": carbon_savings,
                "carbon_emissions_savings_tco2": carbon_savings / 1000.0,
                "carbon_emissions_savings_pct_vs_air_source": _pct(
                    carbon_savings, air_carbon
                ),
            }
        ]
    )


def _float(value: object) -> float:
    return float(value)


def _pct(numerator: float, denominator: object) -> float:
    denominator_float = _float(denominator)
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
    parser.add_argument("--rated-it-power-kw", type=float, default=20000.0)  # 5000  10000  20000  50000
    parser.add_argument("--idle-power-fraction", type=float, default=0.3)
    parser.add_argument("--hours", type=int, default=8760)
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
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
