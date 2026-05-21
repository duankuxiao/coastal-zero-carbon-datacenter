"""Hourly renewable dispatch optimizer for data-center zero-carbon studies.

The optimizer is intentionally deterministic and array-based so it can be used
with project-generated hourly demand, wind, and carbon-intensity profiles
without depending on the reinforcement-learning environments.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from scipy.optimize import linprog
from scipy.sparse import lil_matrix

from core.calculate_datacenter_energy import (
    WORKLOAD_FILE,
    _resolve_aligned_inputs,
    _simulate_datacenter_energy_with_env_model,
)
from core.calculate_wind_capacity import calculate_wind_generation_profile


Objective = Literal["min-grid-mwh", "min-grid-co2"]


@dataclass(frozen=True)
class OptimizationInputs:
    """Inputs for one-hour-step renewable dispatch optimization."""

    demand_mwh: np.ndarray
    wind_mwh: np.ndarray
    carbon_intensity_g_per_kwh: np.ndarray
    battery_capacity_mwh: float
    battery_roundtrip_efficiency: float = 1.0
    grid_import_limit_mw: float | None = None
    battery_charge_limit_mw: float | None = None
    battery_discharge_limit_mw: float | None = None
    load_shift_fraction: float = 0.3


@dataclass(frozen=True)
class OptimizationResult:
    """Optimized hourly dispatch and aggregate metrics."""

    objective: str
    annual_demand_mwh: float
    annual_wind_mwh: float
    grid_purchase_mwh: float
    grid_purchase_co2_kg: float
    average_grid_carbon_intensity_g_per_kwh: float
    renewable_physical_coverage_fraction: float
    wind_curtailment_mwh: float
    battery_charge_mwh: float
    battery_discharge_mwh: float
    battery_conversion_loss_mwh: float
    shifted_down_mwh: float
    shifted_up_mwh: float
    load_movement_budget_used_fraction: float
    hours_with_grid_purchase: int
    hours_with_curtailment: int
    max_hourly_grid_purchase_mw: float
    max_hourly_wind_curtailment_mw: float
    max_hourly_battery_charge_mw: float
    max_hourly_battery_discharge_mw: float
    optimized_demand_mwh: np.ndarray
    grid_purchase_hourly_mwh: np.ndarray
    wind_curtailment_hourly_mwh: np.ndarray
    battery_soc_mwh: np.ndarray
    battery_charge_hourly_mwh: np.ndarray
    battery_discharge_hourly_mwh: np.ndarray

    def to_summary_dict(self) -> dict[str, object]:
        """Return JSON/CSV-friendly scalar metrics."""
        payload = asdict(self)
        for key in list(payload):
            if isinstance(payload[key], np.ndarray):
                payload.pop(key)
        return payload


def optimize_dispatch(
    inputs: OptimizationInputs,
    objective: Objective = "min-grid-mwh",
) -> OptimizationResult:
    """Optimize one-city hourly dispatch.

    The LP uses cyclic storage, one-hour time steps, and a flexible-load model:

    * optimized demand stays within ``(1 +/- load_shift_fraction) * demand``;
    * annual demand is preserved;
    * shifted-down annual energy is limited to ``load_shift_fraction`` of
      annual demand.

    Grid import and battery charge/discharge limits are hourly power limits in
    MW, numerically equal to MWh for one-hour steps.
    """
    data = _validate_inputs(inputs)
    demand = data["demand"]
    wind = data["wind"]
    carbon = data["carbon"]
    n_hours = len(demand)
    total_demand = float(demand.sum())
    eta_charge = math.sqrt(inputs.battery_roundtrip_efficiency)
    eta_discharge = math.sqrt(inputs.battery_roundtrip_efficiency)
    move_fraction = float(inputs.load_shift_fraction)

    idx_x = 0
    idx_red = idx_x + n_hours
    idx_add = idx_red + n_hours
    idx_soc = idx_add + n_hours
    idx_charge = idx_soc + n_hours
    idx_discharge = idx_charge + n_hours
    idx_grid = idx_discharge + n_hours
    idx_curtail = idx_grid + n_hours
    num_vars = idx_curtail + n_hours

    c = np.zeros(num_vars)
    if objective == "min-grid-mwh":
        c[idx_grid : idx_grid + n_hours] = 1.0
    elif objective == "min-grid-co2":
        c[idx_grid : idx_grid + n_hours] = carbon
    else:
        raise ValueError("objective must be 'min-grid-mwh' or 'min-grid-co2'.")

    bounds: list[tuple[float, float | None]] = []
    bounds.extend((0.0, None) for _ in range(n_hours))
    bounds.extend((0.0, float(move_fraction * value)) for value in demand)
    bounds.extend((0.0, float(move_fraction * value)) for value in demand)
    bounds.extend((0.0, float(inputs.battery_capacity_mwh)) for _ in range(n_hours))
    bounds.extend((0.0, inputs.battery_charge_limit_mw) for _ in range(n_hours))
    bounds.extend((0.0, inputs.battery_discharge_limit_mw) for _ in range(n_hours))
    bounds.extend((0.0, inputs.grid_import_limit_mw) for _ in range(n_hours))
    bounds.extend((0.0, None) for _ in range(n_hours))

    num_eq = 3 * n_hours + 1
    a_eq = lil_matrix((num_eq, num_vars), dtype=float)
    b_eq = np.zeros(num_eq)
    row = 0

    for hour in range(n_hours):
        a_eq[row, idx_x + hour] = 1.0
        a_eq[row, idx_red + hour] = 1.0
        a_eq[row, idx_add + hour] = -1.0
        b_eq[row] = demand[hour]
        row += 1

    for hour in range(n_hours):
        a_eq[row, idx_discharge + hour] = 1.0
        a_eq[row, idx_grid + hour] = 1.0
        a_eq[row, idx_x + hour] = -1.0
        a_eq[row, idx_charge + hour] = -1.0
        a_eq[row, idx_curtail + hour] = -1.0
        b_eq[row] = -wind[hour]
        row += 1

    for hour in range(n_hours):
        next_hour = (hour + 1) % n_hours
        a_eq[row, idx_soc + next_hour] = 1.0
        a_eq[row, idx_soc + hour] = -1.0
        a_eq[row, idx_charge + hour] = -eta_charge
        a_eq[row, idx_discharge + hour] = 1.0 / eta_discharge
        row += 1

    for hour in range(n_hours):
        a_eq[row, idx_x + hour] = 1.0
    b_eq[row] = total_demand

    a_ub = lil_matrix((1, num_vars), dtype=float)
    b_ub = np.array([move_fraction * total_demand])
    for hour in range(n_hours):
        a_ub[0, idx_red + hour] = 1.0

    result = linprog(
        c,
        A_ub=a_ub.tocsr(),
        b_ub=b_ub,
        A_eq=a_eq.tocsr(),
        b_eq=b_eq,
        bounds=bounds,
        method="highs",
        options={
            "primal_feasibility_tolerance": 1e-7,
            "dual_feasibility_tolerance": 1e-7,
        },
    )
    if not result.success:
        raise RuntimeError(f"Optimization failed: {result.message}")

    solution = result.x
    optimized_demand = solution[idx_x : idx_x + n_hours]
    soc = solution[idx_soc : idx_soc + n_hours]
    charge = solution[idx_charge : idx_charge + n_hours]
    discharge = solution[idx_discharge : idx_discharge + n_hours]
    grid = solution[idx_grid : idx_grid + n_hours]
    curtail = solution[idx_curtail : idx_curtail + n_hours]

    shifted_down = np.maximum(demand - optimized_demand, 0.0)
    shifted_up = np.maximum(optimized_demand - demand, 0.0)
    grid_purchase_mwh = float(grid.sum())
    grid_co2_kg = float(np.sum(grid * carbon))
    average_grid_ci = grid_co2_kg / grid_purchase_mwh if grid_purchase_mwh else 0.0
    battery_charge_mwh = float(charge.sum())
    battery_discharge_mwh = float(discharge.sum())

    return OptimizationResult(
        objective=objective,
        annual_demand_mwh=total_demand,
        annual_wind_mwh=float(wind.sum()),
        grid_purchase_mwh=grid_purchase_mwh,
        grid_purchase_co2_kg=grid_co2_kg,
        average_grid_carbon_intensity_g_per_kwh=average_grid_ci,
        renewable_physical_coverage_fraction=1.0 - grid_purchase_mwh / total_demand,
        wind_curtailment_mwh=float(curtail.sum()),
        battery_charge_mwh=battery_charge_mwh,
        battery_discharge_mwh=battery_discharge_mwh,
        battery_conversion_loss_mwh=battery_charge_mwh - battery_discharge_mwh,
        shifted_down_mwh=float(shifted_down.sum()),
        shifted_up_mwh=float(shifted_up.sum()),
        load_movement_budget_used_fraction=float(shifted_down.sum() / total_demand),
        hours_with_grid_purchase=int(np.sum(grid > 1e-6)),
        hours_with_curtailment=int(np.sum(curtail > 1e-6)),
        max_hourly_grid_purchase_mw=float(grid.max(initial=0.0)),
        max_hourly_wind_curtailment_mw=float(curtail.max(initial=0.0)),
        max_hourly_battery_charge_mw=float(charge.max(initial=0.0)),
        max_hourly_battery_discharge_mw=float(discharge.max(initial=0.0)),
        optimized_demand_mwh=optimized_demand,
        grid_purchase_hourly_mwh=grid,
        wind_curtailment_hourly_mwh=curtail,
        battery_soc_mwh=soc,
        battery_charge_hourly_mwh=charge,
        battery_discharge_hourly_mwh=discharge,
    )


def build_city_inputs(
    city: str,
    cooling_type: Literal["air_source", "seawater"],
    wind_capacity_mw: float,
    wind_nc_file: str | Path,
    rated_it_power_kw: float = 20000.0,
    hours: int = 8760,
    start_time: str | None = "2025-01-01 00:00",
    battery_capacity_mwh: float = 535.4,
    battery_roundtrip_efficiency: float = 0.97,
    grid_import_limit_mw: float | None = 25.0,
    battery_charge_limit_mw: float | None = 25.0,
    battery_discharge_limit_mw: float | None = 25.0,
    load_shift_fraction: float = 0.3,
) -> OptimizationInputs:
    """Build optimization inputs from this repository's hourly data models."""
    aligned = _resolve_aligned_inputs(
        city=city,
        cooling_type=cooling_type,
        workload_file=WORKLOAD_FILE,
        hours=hours,
        start_time=start_time,
        time_alignment=None,
        max_carbon_gap_hours=6,
        progress=False,
    )
    simulation = _simulate_datacenter_energy_with_env_model(
        workload=aligned["workload"],
        ambient_temperature_c=aligned["ambient_temperature"],
        seawater_temperature_c=aligned["source_temperature"] if cooling_type == "seawater" else None,
        rated_it_power_kw=rated_it_power_kw,
        cooling_type=cooling_type,
        crac_setpoint_c=18.0,
        progress=False,
    )
    demand_mwh = (
        np.asarray(simulation["it_power_kw"], dtype=float)
        + np.asarray(simulation["cooling_power_kw"], dtype=float)
    ) / 1000.0

    wind_profile = calculate_wind_generation_profile(
        input_nc=wind_nc_file,
        capacity_mw=wind_capacity_mw,
        hub_height_m=150.0,
        loss_fraction=0.15,
        cut_in=3.0,
        rated=12.0,
        cut_out=25.0,
    )
    wind_mwh = wind_profile["generation_mwh"].to_numpy(dtype=float)
    if len(wind_mwh) != len(demand_mwh):
        raise ValueError(
            f"Wind and demand lengths differ: wind={len(wind_mwh)}, demand={len(demand_mwh)}."
        )

    return OptimizationInputs(
        demand_mwh=demand_mwh,
        wind_mwh=wind_mwh,
        carbon_intensity_g_per_kwh=np.asarray(aligned["carbon_intensity"], dtype=float),
        battery_capacity_mwh=battery_capacity_mwh,
        battery_roundtrip_efficiency=battery_roundtrip_efficiency,
        grid_import_limit_mw=grid_import_limit_mw,
        battery_charge_limit_mw=battery_charge_limit_mw,
        battery_discharge_limit_mw=battery_discharge_limit_mw,
        load_shift_fraction=load_shift_fraction,
    )


def _validate_inputs(inputs: OptimizationInputs) -> dict[str, np.ndarray]:
    demand = np.asarray(inputs.demand_mwh, dtype=float)
    wind = np.asarray(inputs.wind_mwh, dtype=float)
    carbon = np.asarray(inputs.carbon_intensity_g_per_kwh, dtype=float)
    if demand.ndim != 1 or wind.ndim != 1 or carbon.ndim != 1:
        raise ValueError("demand_mwh, wind_mwh, and carbon_intensity_g_per_kwh must be 1-D.")
    if not (len(demand) == len(wind) == len(carbon)):
        raise ValueError("demand_mwh, wind_mwh, and carbon_intensity_g_per_kwh must have equal length.")
    if len(demand) == 0:
        raise ValueError("Input series must not be empty.")
    if np.any(demand < 0) or np.any(wind < 0) or np.any(carbon < 0):
        raise ValueError("Input series must not contain negative values.")
    if inputs.battery_capacity_mwh < 0:
        raise ValueError("battery_capacity_mwh must be non-negative.")
    if not 0 < inputs.battery_roundtrip_efficiency <= 1:
        raise ValueError("battery_roundtrip_efficiency must be in the range (0, 1].")
    if not 0 <= inputs.load_shift_fraction < 1:
        raise ValueError("load_shift_fraction must be in the range [0, 1).")
    for label, value in {
        "grid_import_limit_mw": inputs.grid_import_limit_mw,
        "battery_charge_limit_mw": inputs.battery_charge_limit_mw,
        "battery_discharge_limit_mw": inputs.battery_discharge_limit_mw,
    }.items():
        if value is not None and value < 0:
            raise ValueError(f"{label} must be non-negative when provided.")
    return {"demand": demand, "wind": wind, "carbon": carbon}


def _json_ready(value: object) -> object:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Optimize hourly renewable dispatch for a data-center city."
    )
    parser.add_argument("--city", default="Shanghai")
    parser.add_argument("--cooling", choices=["air_source", "seawater"], default="seawater")
    parser.add_argument("--wind-capacity-mw", type=float, required=True)
    parser.add_argument("--wind-nc-file", required=True)
    parser.add_argument("--battery-capacity-mwh", type=float, default=535.4)
    parser.add_argument("--battery-roundtrip-efficiency", type=float, default=0.97)
    parser.add_argument("--grid-import-limit-mw", type=float, default=25.0)
    parser.add_argument("--battery-charge-limit-mw", type=float, default=25.0)
    parser.add_argument("--battery-discharge-limit-mw", type=float, default=25.0)
    parser.add_argument("--load-shift-fraction", type=float, default=0.3)
    parser.add_argument("--hours", type=int, default=8760)
    parser.add_argument("--start-time", default="2025-01-01 00:00")
    parser.add_argument("--objective", choices=["min-grid-mwh", "min-grid-co2"], default="min-grid-mwh")
    parser.add_argument("--include-hourly", action="store_true")
    args = parser.parse_args()

    inputs = build_city_inputs(
        city=args.city,
        cooling_type=args.cooling,
        wind_capacity_mw=args.wind_capacity_mw,
        wind_nc_file=args.wind_nc_file,
        battery_capacity_mwh=args.battery_capacity_mwh,
        battery_roundtrip_efficiency=args.battery_roundtrip_efficiency,
        grid_import_limit_mw=args.grid_import_limit_mw,
        battery_charge_limit_mw=args.battery_charge_limit_mw,
        battery_discharge_limit_mw=args.battery_discharge_limit_mw,
        load_shift_fraction=args.load_shift_fraction,
        hours=args.hours,
        start_time=args.start_time,
    )
    result = optimize_dispatch(inputs, objective=args.objective)
    payload = asdict(result) if args.include_hourly else result.to_summary_dict()
    print(json.dumps(payload, default=_json_ready, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
