"""Hourly seawater cooling and water-source heat-pump model.

The public entry point is :func:`calculate_seawater_cooling`.  The module keeps
the calculation broken into engineering submodels so the assumptions can be
calibrated independently from ``utils/dc_config.json``.
"""

from __future__ import annotations

import math
from typing import Any


GRAVITY_M_PER_S2 = 9.80665


def _cfg(config: Any, names: str | tuple[str, ...], default: Any) -> Any:
    if not isinstance(names, tuple):
        names = (names,)
    for name in names:
        if config is not None and hasattr(config, name):
            value = getattr(config, name)
            if value is not None:
                return value
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _poly(coefficients: Any, x: float, default: float = 1.0) -> float:
    if not coefficients:
        return default
    try:
        return float(sum(float(c) * (x ** i) for i, c in enumerate(coefficients)))
    except (TypeError, ValueError):
        return default


def _biquadratic(coefficients: Any, x: float, y: float, default: float = 1.0) -> float:
    if not coefficients:
        return default
    try:
        c = [float(item) for item in coefficients]
    except (TypeError, ValueError):
        return default
    if len(c) < 6:
        return default
    return c[0] + c[1] * x + c[2] * x * x + c[3] * y + c[4] * y * y + c[5] * x * y


def _seawater_density(config: Any) -> float:
    return max(_as_float(_cfg(config, "SEAWATER_DENSITY_KG_PER_M3", 1025.0), 1025.0), 1.0)


def _seawater_cp(config: Any) -> float:
    return max(_as_float(_cfg(config, "SEAWATER_CP_J_PER_KG_K", 3990.0), 3990.0), 1.0)


def _water_density(config: Any) -> float:
    return max(_as_float(_cfg(config, "CHILLED_WATER_DENSITY_KG_PER_M3", 997.0), 997.0), 1.0)


def _water_cp(config: Any) -> float:
    return max(_as_float(_cfg(config, "CHILLED_WATER_CP_J_PER_KG_K", 4180.0), 4180.0), 1.0)


def chilled_water_loop(
    cooling_load_w: float,
    config: Any,
    base_pump_power_w: float = 0.0,
) -> dict[str, float]:
    """Return chilled-water flow, pump power, and supply/return temperatures."""
    load_w = max(float(cooling_load_w), 0.0)
    supply_temp_c = _as_float(
        _cfg(config, "SEAWATER_CHILLED_WATER_SUPPLY_TEMP_C", 12.0), 12.0
    )
    delta_t_c = max(
        _as_float(
            _cfg(
                config,
                ("SEAWATER_CHILLED_WATER_DELTA_T_C", "SEAWATER_DELTA_T_C"),
                5.0,
            ),
            5.0,
        ),
        0.1,
    )
    density = _water_density(config)
    cp = _water_cp(config)
    flow_m3_s = load_w / (density * cp * delta_t_c) if load_w > 0 else 0.0

    configured_design_flow = _as_float(
        _cfg(
            config,
            ("SEAWATER_CHILLED_WATER_DESIGN_FLOW_M3_S", "CW_WATER_FLOW_RATE"),
            flow_m3_s,
        ),
        flow_m3_s,
    )
    design_flow_m3_s = max(configured_design_flow, flow_m3_s, 1e-9)
    flow_fraction = _clip(flow_m3_s / design_flow_m3_s, 0.0, 1.5)

    variable_speed = _as_bool(_cfg(config, "SEAWATER_CHILLED_WATER_VARIABLE_SPEED_PUMP", True), True)
    if base_pump_power_w > 0:
        pump_power_w = base_pump_power_w * (flow_fraction ** 3 if variable_speed else 1.0)
    else:
        pressure_drop_pa = _as_float(
            _cfg(config, ("SEAWATER_CHILLED_WATER_PRESSURE_DROP_PA", "CW_PRESSURE_DROP"), 300000.0),
            300000.0,
        )
        efficiency = max(
            _as_float(_cfg(config, ("SEAWATER_CHILLED_WATER_PUMP_EFFICIENCY", "CW_PUMP_EFFICIENCY"), 0.87), 0.87),
            0.01,
        )
        pump_power_w = pressure_drop_pa * flow_m3_s / efficiency

    return {
        "cooling_load_w": load_w,
        "flow_m3_s": flow_m3_s,
        "design_flow_m3_s": design_flow_m3_s,
        "flow_fraction": flow_fraction,
        "pump_power_w": pump_power_w if load_w > 0 else 0.0,
        "supply_temp_c": supply_temp_c,
        "return_temp_c": supply_temp_c + delta_t_c if load_w > 0 else supply_temp_c,
        "delta_t_c": delta_t_c,
    }


def seawater_intake_loop(
    heat_rejection_w: float,
    source_entering_temp_c: float,
    config: Any,
) -> dict[str, float | bool]:
    """Return seawater flow, pressure drop, pump power, and outfall diagnostics."""
    heat_w = max(float(heat_rejection_w), 0.0)
    source_temp_c = float(source_entering_temp_c)
    density = _seawater_density(config)
    cp = _seawater_cp(config)
    design_delta_t_c = max(_as_float(_cfg(config, "SEAWATER_DELTA_T_C", 5.0), 5.0), 0.1)
    max_rise_c = max(
        _as_float(_cfg(config, "SEAWATER_MAX_OUTFALL_TEMPERATURE_RISE_C", design_delta_t_c), design_delta_t_c),
        0.1,
    )
    min_flow_m3_s = max(_as_float(_cfg(config, "SEAWATER_MIN_FLOW_M3_S", 0.0), 0.0), 0.0)
    max_flow_m3_s = max(
        _as_float(_cfg(config, "SEAWATER_MAX_FLOW_M3_S", math.inf), math.inf),
        min_flow_m3_s,
    )

    if heat_w <= 0:
        flow_m3_s = 0.0
    else:
        flow_for_design_delta = heat_w / (density * cp * design_delta_t_c)
        flow_for_outfall_limit = heat_w / (density * cp * max_rise_c)
        flow_m3_s = max(flow_for_design_delta, flow_for_outfall_limit, min_flow_m3_s)
        flow_m3_s = min(flow_m3_s, max_flow_m3_s)

    outfall_rise_c = heat_w / (density * cp * flow_m3_s) if flow_m3_s > 0 else 0.0
    outfall_violation = heat_w > 0 and outfall_rise_c > max_rise_c + 1e-9

    temp_min_c = _as_float(_cfg(config, "SEAWATER_MIN_TEMPERATURE_C", -2.0), -2.0)
    temp_max_c = _as_float(_cfg(config, "SEAWATER_MAX_TEMPERATURE_C", 35.0), 35.0)
    temp_violation = source_temp_c < temp_min_c or source_temp_c > temp_max_c

    pressure_drop_pa = _seawater_pressure_drop_pa(flow_m3_s, config)
    design_default_flow = max(max_flow_m3_s, flow_m3_s, 1e-9) if math.isfinite(max_flow_m3_s) else max(flow_m3_s, 1e-9)
    design_flow_m3_s = _as_float(
        _cfg(config, "SEAWATER_DESIGN_FLOW_M3_S", design_default_flow),
        design_default_flow,
    )
    flow_fraction = _clip(flow_m3_s / max(design_flow_m3_s, flow_m3_s, 1e-9), 0.0, 1.5)
    base_efficiency = max(_as_float(_cfg(config, "SEAWATER_PUMP_EFFICIENCY", 0.80), 0.80), 0.01)
    efficiency_curve = _cfg(config, "SEAWATER_PUMP_EFFICIENCY_CURVE", None)
    pump_efficiency = _clip(_poly(efficiency_curve, flow_fraction, base_efficiency), 0.01, 1.0)

    variable_speed = _as_bool(_cfg(config, "SEAWATER_VARIABLE_SPEED_PUMP", True), True)
    if variable_speed and _uses_fixed_pressure_drop(config):
        base_pressure_drop_pa = _as_float(
            _cfg(config, ("SEAWATER_FIXED_PRESSURE_DROP_PA", "SEAWATER_PUMP_PRESSURE_DROP_PA"), 250000.0),
            250000.0,
        )
        base_pump_power_w = base_pressure_drop_pa * max(design_flow_m3_s, flow_m3_s) / pump_efficiency
        pump_power_w = base_pump_power_w * flow_fraction ** 3
    else:
        pump_power_w = pressure_drop_pa * flow_m3_s / pump_efficiency if flow_m3_s > 0 else 0.0

    return {
        "source_entering_temp_c": source_temp_c,
        "source_leaving_temp_c": source_temp_c + outfall_rise_c,
        "flow_m3_s": flow_m3_s,
        "design_flow_m3_s": design_flow_m3_s,
        "flow_fraction": flow_fraction,
        "pressure_drop_pa": pressure_drop_pa,
        "pump_efficiency": pump_efficiency,
        "pump_power_w": pump_power_w if heat_w > 0 else 0.0,
        "outfall_temperature_rise_c": outfall_rise_c,
        "constraint_violation": bool(outfall_violation or temp_violation),
        "outfall_temperature_violation": bool(outfall_violation),
        "seawater_temperature_violation": bool(temp_violation),
    }


def _uses_fixed_pressure_drop(config: Any) -> bool:
    pipe_length_m = _as_float(_cfg(config, "SEAWATER_PIPE_LENGTH_M", 0.0), 0.0)
    pipe_diameter_m = _as_float(_cfg(config, "SEAWATER_PIPE_DIAMETER_M", 0.0), 0.0)
    return pipe_length_m <= 0 or pipe_diameter_m <= 0


def _seawater_pressure_drop_pa(flow_m3_s: float, config: Any) -> float:
    fixed_pressure_drop_pa = _as_float(
        _cfg(config, ("SEAWATER_FIXED_PRESSURE_DROP_PA", "SEAWATER_PUMP_PRESSURE_DROP_PA"), 250000.0),
        250000.0,
    )
    pipe_length_m = _as_float(_cfg(config, "SEAWATER_PIPE_LENGTH_M", 0.0), 0.0)
    pipe_diameter_m = _as_float(_cfg(config, "SEAWATER_PIPE_DIAMETER_M", 0.0), 0.0)
    if flow_m3_s <= 0:
        return 0.0
    if pipe_length_m <= 0 or pipe_diameter_m <= 0:
        return max(fixed_pressure_drop_pa, 0.0)

    density = _seawater_density(config)
    dynamic_viscosity_pa_s = max(
        _as_float(_cfg(config, "SEAWATER_DYNAMIC_VISCOSITY_PA_S", 0.00108), 0.00108),
        1e-9,
    )
    roughness_m = max(_as_float(_cfg(config, "SEAWATER_ROUGHNESS_M", 0.000045), 0.000045), 0.0)
    area_m2 = math.pi * (pipe_diameter_m ** 2) / 4.0
    velocity_m_s = flow_m3_s / max(area_m2, 1e-12)
    reynolds = density * velocity_m_s * pipe_diameter_m / dynamic_viscosity_pa_s
    if reynolds <= 0:
        friction_factor = 0.0
    elif reynolds < 2300:
        friction_factor = 64.0 / reynolds
    else:
        relative_roughness = roughness_m / pipe_diameter_m
        friction_factor = 0.25 / (
            math.log10(relative_roughness / 3.7 + 5.74 / (reynolds ** 0.9)) ** 2
        )

    friction_drop_pa = friction_factor * (pipe_length_m / pipe_diameter_m) * density * velocity_m_s ** 2 / 2.0
    static_head_m = _as_float(_cfg(config, "SEAWATER_STATIC_HEAD_M", 0.0), 0.0)
    static_drop_pa = density * GRAVITY_M_PER_S2 * max(static_head_m, 0.0)
    filter_drop_pa = max(_as_float(_cfg(config, "SEAWATER_FILTER_PRESSURE_DROP_PA", 0.0), 0.0), 0.0)
    fouling_drop_pa = max(_as_float(_cfg(config, "SEAWATER_FOULING_PRESSURE_DROP_PA", 0.0), 0.0), 0.0)
    return max(fixed_pressure_drop_pa, 0.0) + friction_drop_pa + static_drop_pa + filter_drop_pa + fouling_drop_pa


def plate_heat_exchanger(
    cooling_load_w: float,
    source_entering_temp_c: float,
    chilled_water_return_temp_c: float,
    chilled_water_supply_temp_c: float,
    seawater_flow_m3_s: float,
    chilled_water_flow_m3_s: float,
    config: Any,
) -> dict[str, float]:
    """Calculate heat-exchanger capacity with effectiveness-NTU or configured effectiveness."""
    load_w = max(float(cooling_load_w), 0.0)
    density_source = _seawater_density(config)
    cp_source = _seawater_cp(config)
    density_load = _water_density(config)
    cp_load = _water_cp(config)
    c_source_w_per_k = max(seawater_flow_m3_s, 0.0) * density_source * cp_source
    c_load_w_per_k = max(chilled_water_flow_m3_s, 0.0) * density_load * cp_load
    temp_delta_c = chilled_water_return_temp_c - source_entering_temp_c

    if load_w <= 0 or c_source_w_per_k <= 0 or c_load_w_per_k <= 0 or temp_delta_c <= 0:
        return {
            "capacity_w": 0.0,
            "cooling_load_served_w": 0.0,
            "effectiveness": 0.0,
            "ntu": 0.0,
            "source_leaving_temp_c": source_entering_temp_c,
            "chilled_water_leaving_temp_c": chilled_water_return_temp_c,
            "approach_temperature_c": chilled_water_return_temp_c - source_entering_temp_c,
        }

    c_min = min(c_source_w_per_k, c_load_w_per_k)
    c_max = max(c_source_w_per_k, c_load_w_per_k)
    capacity_rate_ratio = c_min / c_max if c_max > 0 else 0.0
    ua_w_per_k = _as_float(_cfg(config, "SEAWATER_HEAT_EXCHANGER_UA_W_PER_K", 0.0), 0.0)
    fouling_factor = max(_as_float(_cfg(config, "SEAWATER_FOULING_FACTOR_M2K_PER_W", 0.0), 0.0), 0.0)
    if ua_w_per_k > 0:
        ua_effective = ua_w_per_k / (1.0 + ua_w_per_k * fouling_factor)
        ntu = ua_effective / c_min
        if abs(1.0 - capacity_rate_ratio) < 1e-9:
            effectiveness = ntu / (1.0 + ntu)
        else:
            numerator = 1.0 - math.exp(-ntu * (1.0 - capacity_rate_ratio))
            denominator = 1.0 - capacity_rate_ratio * math.exp(-ntu * (1.0 - capacity_rate_ratio))
            effectiveness = numerator / max(denominator, 1e-12)
    else:
        ntu = 0.0
        effectiveness = _as_float(_cfg(config, "SEAWATER_HEAT_EXCHANGER_EFFECTIVENESS", 0.75), 0.75)

    effectiveness = _clip(effectiveness, 0.0, 1.0)
    capacity_w = effectiveness * c_min * temp_delta_c
    served_w = min(load_w, max(capacity_w, 0.0))
    source_leaving_temp_c = source_entering_temp_c + served_w / c_source_w_per_k
    chilled_water_leaving_temp_c = chilled_water_return_temp_c - served_w / c_load_w_per_k

    return {
        "capacity_w": max(capacity_w, 0.0),
        "cooling_load_served_w": served_w,
        "effectiveness": effectiveness,
        "ntu": ntu,
        "source_leaving_temp_c": source_leaving_temp_c,
        "chilled_water_leaving_temp_c": chilled_water_leaving_temp_c,
        "approach_temperature_c": chilled_water_leaving_temp_c - source_leaving_temp_c,
    }


def heat_pump_chiller(
    cooling_load_w: float,
    source_entering_temp_c: float,
    chilled_water_supply_temp_c: float,
    source_flow_fraction: float,
    load_flow_fraction: float,
    config: Any,
) -> dict[str, float | str]:
    """Water-source heat-pump/chiller model with curve-fit and Carnot fallback."""
    load_w = max(float(cooling_load_w), 0.0)
    if load_w <= 0:
        return {
            "available_capacity_w": 0.0,
            "cooling_load_served_w": 0.0,
            "unmet_cooling_load_w": 0.0,
            "compressor_power_w": 0.0,
            "effective_cop": math.inf,
            "compressor_cop": math.inf,
            "heat_rejection_w": 0.0,
            "part_load_ratio": 0.0,
            "cycling_ratio": 0.0,
            "cop_model": "off",
        }

    curve = _performance_curve(config)
    rated_capacity_w = _as_float(
        _cfg(config, "SEAWATER_HEAT_PUMP_RATED_CAPACITY_W", load_w), load_w
    )
    rated_capacity_w = max(rated_capacity_w, load_w if rated_capacity_w <= 0 else rated_capacity_w)
    source_ff = _clip(float(source_flow_fraction), 0.0, 1.5)
    load_ff = _clip(float(load_flow_fraction), 0.0, 1.5)

    if curve:
        available_capacity_w = _curve_fit_available_capacity_w(
            curve, rated_capacity_w, source_entering_temp_c, chilled_water_supply_temp_c, source_ff, load_ff
        )
        available_capacity_w = max(available_capacity_w, 0.0)
        served_w = min(load_w, available_capacity_w)
        actual_plr = served_w / available_capacity_w if available_capacity_w > 0 else 0.0
        min_plr = _clip(_as_float(curve.get("minimum_part_load_ratio"), 0.10), 0.01, 1.0)
        operating_plr = _clip(actual_plr, min_plr, 1.0) if served_w > 0 else 0.0
        cycling_ratio = _clip(actual_plr / min_plr, 0.0, 1.0) if served_w > 0 and actual_plr < min_plr else (1.0 if served_w > 0 else 0.0)
        compressor_cop = _curve_fit_cop(
            curve,
            source_entering_temp_c,
            chilled_water_supply_temp_c,
            operating_plr,
            source_ff,
            load_ff,
            config,
        )
        cop_model = "curve_fit"
    else:
        available_capacity_w = _as_float(
            _cfg(config, "SEAWATER_HEAT_PUMP_RATED_CAPACITY_W", load_w), load_w
        )
        available_capacity_w = max(available_capacity_w, load_w)
        served_w = min(load_w, available_capacity_w)
        actual_plr = served_w / available_capacity_w if available_capacity_w > 0 else 0.0
        min_plr = _clip(_as_float(_cfg(config, "SEAWATER_HEAT_PUMP_MIN_PLR", 0.10), 0.10), 0.01, 1.0)
        cycling_ratio = _clip(actual_plr / min_plr, 0.0, 1.0) if served_w > 0 and actual_plr < min_plr else (1.0 if served_w > 0 else 0.0)
        compressor_cop = _carnot_cop(source_entering_temp_c, chilled_water_supply_temp_c, config)
        cop_model = "carnot"

    compressor_power_w = served_w / compressor_cop if served_w > 0 and compressor_cop > 0 else 0.0
    compressor_power_w *= cycling_ratio if 0.0 < cycling_ratio < 1.0 else 1.0
    heat_rejection_w = served_w + compressor_power_w
    unmet_w = max(load_w - served_w, 0.0)

    return {
        "available_capacity_w": available_capacity_w,
        "cooling_load_served_w": served_w,
        "unmet_cooling_load_w": unmet_w,
        "compressor_power_w": compressor_power_w,
        "effective_cop": served_w / compressor_power_w if compressor_power_w > 0 else math.inf,
        "compressor_cop": compressor_cop,
        "heat_rejection_w": heat_rejection_w,
        "part_load_ratio": actual_plr,
        "cycling_ratio": cycling_ratio,
        "cop_model": cop_model,
    }


def _performance_curve(config: Any) -> dict[str, Any] | None:
    curve = _cfg(
        config,
        ("SEAWATER_PERFORMANCE_CURVE", "SEAWATER_HEAT_PUMP_PERFORMANCE_CURVE"),
        None,
    )
    if not isinstance(curve, dict) or not curve:
        return None
    required_keys = {
        "capacity_temperature_coefficients",
        "eir_temperature_coefficients",
        "eir_part_load_coefficients",
    }
    if not any(key in curve for key in required_keys):
        return None
    return curve


def _curve_fit_available_capacity_w(
    curve: dict[str, Any],
    rated_capacity_w: float,
    source_entering_temp_c: float,
    chilled_water_supply_temp_c: float,
    source_flow_fraction: float,
    load_flow_fraction: float,
) -> float:
    capacity_temp = _biquadratic(
        curve.get("capacity_temperature_coefficients"),
        source_entering_temp_c,
        chilled_water_supply_temp_c,
        default=1.0,
    )
    source_flow = _poly(curve.get("capacity_source_flow_coefficients"), source_flow_fraction, 1.0)
    load_flow = _poly(curve.get("capacity_load_flow_coefficients"), load_flow_fraction, 1.0)
    multiplier = _clip(capacity_temp * source_flow * load_flow, 0.05, 2.5)
    return rated_capacity_w * multiplier


def _curve_fit_cop(
    curve: dict[str, Any],
    source_entering_temp_c: float,
    chilled_water_supply_temp_c: float,
    operating_part_load_ratio: float,
    source_flow_fraction: float,
    load_flow_fraction: float,
    config: Any,
) -> float:
    rated_cop = max(_as_float(curve.get("rated_cop"), _as_float(_cfg(config, "SEAWATER_HEAT_PUMP_RATED_COP", 6.0), 6.0)), 0.1)
    eir_temp = _biquadratic(
        curve.get("eir_temperature_coefficients"),
        source_entering_temp_c,
        chilled_water_supply_temp_c,
        default=1.0,
    )
    eir_plr = _poly(curve.get("eir_part_load_coefficients"), operating_part_load_ratio, 1.0)
    eir_source_flow = _poly(curve.get("eir_source_flow_coefficients"), source_flow_fraction, 1.0)
    eir_load_flow = _poly(curve.get("eir_load_flow_coefficients"), load_flow_fraction, 1.0)
    eir_multiplier = _clip(eir_temp * eir_plr * eir_source_flow * eir_load_flow, 0.10, 5.0)
    cop = rated_cop / eir_multiplier
    return _clip(
        cop,
        _as_float(_cfg(config, "SEAWATER_MIN_COP", 2.5), 2.5),
        _as_float(_cfg(config, "SEAWATER_MAX_COP", 12.0), 12.0),
    )


def _carnot_cop(source_entering_temp_c: float, chilled_water_supply_temp_c: float, config: Any) -> float:
    evaporator_approach_c = _as_float(_cfg(config, "SEAWATER_EVAPORATOR_APPROACH_C", 2.0), 2.0)
    heat_pump_approach_c = _as_float(_cfg(config, "SEAWATER_HEAT_PUMP_APPROACH_C", 5.0), 5.0)
    min_lift_c = max(_as_float(_cfg(config, "SEAWATER_MIN_TEMP_LIFT_C", 3.0), 3.0), 0.1)
    carnot_efficiency = _as_float(_cfg(config, "SEAWATER_COP_CARNOT_EFFICIENCY", 0.45), 0.45)
    evap_temp_k = chilled_water_supply_temp_c - evaporator_approach_c + 273.15
    condenser_temp_k = source_entering_temp_c + heat_pump_approach_c + 273.15
    temp_lift_k = max(condenser_temp_k - evap_temp_k, min_lift_c)
    ideal_cop = evap_temp_k / temp_lift_k
    return _clip(
        carnot_efficiency * ideal_cop,
        _as_float(_cfg(config, "SEAWATER_MIN_COP", 2.5), 2.5),
        _as_float(_cfg(config, "SEAWATER_MAX_COP", 12.0), 12.0),
    )


def controls(
    cooling_load_w: float,
    heat_exchanger_capacity_w: float,
    previous_mode: str | None = None,
    hysteresis_fraction: float = 0.05,
) -> str:
    """Choose free, hybrid, or mechanical mode from heat-exchanger capability."""
    load_w = max(float(cooling_load_w), 0.0)
    if load_w <= 0:
        return "off"

    hx_fraction = max(float(heat_exchanger_capacity_w), 0.0) / load_w
    hysteresis = _clip(hysteresis_fraction, 0.0, 0.5)

    if hx_fraction >= 1.0:
        return "free_cooling"
    if previous_mode == "mechanical_heat_pump" and hx_fraction <= hysteresis:
        return "mechanical_heat_pump"

    if hx_fraction > 0.0:
        return "hybrid_cooling"
    return "mechanical_heat_pump"


def calculate_seawater_cooling(
    CRAC_cooling_load: float,
    CRAC_Fan_load: float,
    CW_pump_load: float,
    seawater_temp: float | None,
    DC_Config: Any,
) -> dict[str, float | bool | str]:
    """Calculate detailed seawater cooling power while preserving legacy keys."""
    source_temp_c = (
        _as_float(_cfg(DC_Config, "SEAWATER_DEFAULT_TEMP_C", 15.0), 15.0)
        if seawater_temp is None
        else float(seawater_temp)
    )
    cooling_load_w = max(float(CRAC_cooling_load), 0.0)
    crac_fan_w = max(float(CRAC_Fan_load), 0.0)
    base_chilled_pump_w = max(float(CW_pump_load), 0.0)

    full_load_chilled_loop = chilled_water_loop(cooling_load_w, DC_Config, base_chilled_pump_w)
    candidate_source_loop = seawater_intake_loop(cooling_load_w, source_temp_c, DC_Config)
    heat_exchanger = plate_heat_exchanger(
        cooling_load_w=cooling_load_w,
        source_entering_temp_c=source_temp_c,
        chilled_water_return_temp_c=full_load_chilled_loop["return_temp_c"],
        chilled_water_supply_temp_c=full_load_chilled_loop["supply_temp_c"],
        seawater_flow_m3_s=float(candidate_source_loop["flow_m3_s"]),
        chilled_water_flow_m3_s=full_load_chilled_loop["flow_m3_s"],
        config=DC_Config,
    )

    previous_mode = _cfg(DC_Config, "_SEAWATER_PREVIOUS_MODE", None)
    hysteresis_fraction = _as_float(
        _cfg(DC_Config, "SEAWATER_CONTROL_HYSTERESIS_FRACTION", 0.05), 0.05
    )
    cooling_mode = controls(
        cooling_load_w,
        heat_exchanger["capacity_w"],
        previous_mode=previous_mode,
        hysteresis_fraction=hysteresis_fraction,
    )
    if DC_Config is not None:
        try:
            setattr(DC_Config, "_SEAWATER_PREVIOUS_MODE", cooling_mode)
        except Exception:
            pass

    if cooling_mode == "free_cooling":
        free_cooling_served_w = cooling_load_w
    elif cooling_mode == "hybrid_cooling":
        free_cooling_served_w = min(cooling_load_w, heat_exchanger["capacity_w"])
    else:
        free_cooling_served_w = 0.0

    mechanical_load_w = max(cooling_load_w - free_cooling_served_w, 0.0)
    heat_pump = heat_pump_chiller(
        cooling_load_w=mechanical_load_w,
        source_entering_temp_c=source_temp_c,
        chilled_water_supply_temp_c=full_load_chilled_loop["supply_temp_c"],
        source_flow_fraction=float(candidate_source_loop["flow_fraction"]),
        load_flow_fraction=full_load_chilled_loop["flow_fraction"],
        config=DC_Config,
    )

    mechanical_served_w = float(heat_pump["cooling_load_served_w"])
    cooling_served_w = free_cooling_served_w + mechanical_served_w
    unmet_w = max(cooling_load_w - cooling_served_w, 0.0)
    compressor_power_w = float(heat_pump["compressor_power_w"])
    heat_rejection_w = free_cooling_served_w + float(heat_pump["heat_rejection_w"])
    final_source_loop = seawater_intake_loop(heat_rejection_w, source_temp_c, DC_Config)
    final_chilled_loop = chilled_water_loop(cooling_served_w, DC_Config, base_chilled_pump_w)
    heat_exchanger_aux_power_w = _as_float(
        _cfg(DC_Config, "SEAWATER_AUX_POWER_RATIO", 0.01), 0.01
    ) * free_cooling_served_w

    cooling_system_power_w = (
        compressor_power_w
        + float(final_source_loop["pump_power_w"])
        + final_chilled_loop["pump_power_w"]
        + heat_exchanger_aux_power_w
    )
    total_power_w = crac_fan_w + cooling_system_power_w
    effective_cop = cooling_served_w / cooling_system_power_w if cooling_system_power_w > 0 else math.inf
    compressor_cop = float(heat_pump["compressor_cop"])
    cooling_cop = compressor_cop if mechanical_served_w > 0 else math.inf

    mode = _legacy_mode_name(cooling_mode)
    free_fraction = free_cooling_served_w / cooling_load_w if cooling_load_w > 0 else 0.0
    mechanical_fraction = mechanical_served_w / cooling_load_w if cooling_load_w > 0 else 0.0

    return {
        "mode": mode,
        "cooling_mode": cooling_mode,
        "seawater_temp": source_temp_c,
        "free_cooling_active": free_cooling_served_w > 0,
        "cooling_cop": cooling_cop,
        "compressor_cop": compressor_cop,
        "compressor_power": compressor_power_w,
        "compressor_power_w": compressor_power_w,
        "seawater_pump_power": float(final_source_loop["pump_power_w"]),
        "seawater_aux_power": heat_exchanger_aux_power_w,
        "heat_exchanger_aux_power_w": heat_exchanger_aux_power_w,
        "chilled_water_pump_power_w": final_chilled_loop["pump_power_w"],
        "seawater_flow_rate_m3_s": float(final_source_loop["flow_m3_s"]),
        "total_power": total_power_w,
        "available_capacity_w": free_cooling_served_w + float(heat_pump["available_capacity_w"]),
        "heat_exchanger_capacity_w": heat_exchanger["capacity_w"],
        "heat_pump_available_capacity_w": float(heat_pump["available_capacity_w"]),
        "free_cooling_served_w": free_cooling_served_w,
        "mechanical_cooling_served_w": mechanical_served_w,
        "cooling_load_served_w": cooling_served_w,
        "unmet_cooling_load_w": unmet_w,
        "effective_cop": effective_cop,
        "heat_rejection_w": heat_rejection_w,
        "part_load_ratio": float(heat_pump["part_load_ratio"]),
        "cycling_ratio": float(heat_pump["cycling_ratio"]),
        "source_entering_temp_c": source_temp_c,
        "source_leaving_temp_c": float(final_source_loop["source_leaving_temp_c"]),
        "chilled_water_supply_temp_c": final_chilled_loop["supply_temp_c"],
        "chilled_water_return_temp_c": final_chilled_loop["return_temp_c"],
        "free_cooling_fraction": free_fraction,
        "mechanical_cooling_fraction": mechanical_fraction,
        "heat_exchanger_effectiveness": heat_exchanger["effectiveness"],
        "heat_exchanger_ntu": heat_exchanger["ntu"],
        "heat_exchanger_approach_temperature_c": heat_exchanger["approach_temperature_c"],
        "outfall_temperature_rise_c": float(final_source_loop["outfall_temperature_rise_c"]),
        "source_pressure_drop_pa": float(final_source_loop["pressure_drop_pa"]),
        "source_pump_efficiency": float(final_source_loop["pump_efficiency"]),
        "source_flow_fraction": float(final_source_loop["flow_fraction"]),
        "load_flow_fraction": final_chilled_loop["flow_fraction"],
        "constraint_violation": bool(final_source_loop["constraint_violation"]),
        "cop_model": str(heat_pump["cop_model"]),
    }


def _legacy_mode_name(cooling_mode: str) -> str:
    mapping = {
        "off": "seawater_off",
        "free_cooling": "seawater_free_cooling",
        "hybrid_cooling": "seawater_hybrid_cooling",
        "mechanical_heat_pump": "seawater_heat_pump",
    }
    return mapping.get(cooling_mode, f"seawater_{cooling_mode}")


def default_seawater_cooling_result(seawater_temp: float | None = None) -> dict[str, float | bool | str]:
    """Return a zeroed result with every detailed seawater key present."""
    source_temp = math.nan if seawater_temp is None else float(seawater_temp)
    return {
        "mode": "not_used",
        "cooling_mode": "not_used",
        "seawater_temp": source_temp,
        "free_cooling_active": False,
        "cooling_cop": math.nan,
        "compressor_cop": math.nan,
        "compressor_power": 0.0,
        "compressor_power_w": 0.0,
        "seawater_pump_power": 0.0,
        "seawater_aux_power": 0.0,
        "heat_exchanger_aux_power_w": 0.0,
        "chilled_water_pump_power_w": 0.0,
        "seawater_flow_rate_m3_s": 0.0,
        "total_power": 0.0,
        "available_capacity_w": 0.0,
        "heat_exchanger_capacity_w": 0.0,
        "heat_pump_available_capacity_w": 0.0,
        "free_cooling_served_w": 0.0,
        "mechanical_cooling_served_w": 0.0,
        "cooling_load_served_w": 0.0,
        "unmet_cooling_load_w": 0.0,
        "effective_cop": math.nan,
        "heat_rejection_w": 0.0,
        "part_load_ratio": 0.0,
        "cycling_ratio": 0.0,
        "source_entering_temp_c": source_temp,
        "source_leaving_temp_c": source_temp,
        "chilled_water_supply_temp_c": math.nan,
        "chilled_water_return_temp_c": math.nan,
        "free_cooling_fraction": 0.0,
        "mechanical_cooling_fraction": 0.0,
        "heat_exchanger_effectiveness": 0.0,
        "heat_exchanger_ntu": 0.0,
        "heat_exchanger_approach_temperature_c": math.nan,
        "outfall_temperature_rise_c": 0.0,
        "source_pressure_drop_pa": 0.0,
        "source_pump_efficiency": 0.0,
        "source_flow_fraction": 0.0,
        "load_flow_fraction": 0.0,
        "constraint_violation": False,
        "cop_model": "not_used",
    }
