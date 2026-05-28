import pytest

from energy.seawater_heat_pump import heat_pump_chiller, seawater_intake_loop


class _Config:
    SEAWATER_HEAT_PUMP_RATED_CAPACITY_W = 0.0
    SEAWATER_HEAT_PUMP_RATED_COP = 6.0
    SEAWATER_HEAT_PUMP_MIN_PLR = 0.10
    SEAWATER_MIN_COP = 2.5
    SEAWATER_MAX_COP = 12.0
    SEAWATER_HEAT_PUMP_PERFORMANCE_CURVE = {
        "rated_cop": 6.0,
        "minimum_part_load_ratio": 0.10,
        "capacity_temperature_coefficients": [1.08, -0.004, 0.0, 0.0, 0.0, 0.0],
        "capacity_source_flow_coefficients": [0.0, 1.0],
        "capacity_load_flow_coefficients": [0.0, 1.0],
        "eir_temperature_coefficients": [0.72, 0.018, 0.0, 0.0, 0.0, 0.0],
        "eir_part_load_coefficients": [0.15, 0.75, 0.10],
        "eir_source_flow_coefficients": [1.10, -0.10],
        "eir_load_flow_coefficients": [1.05, -0.05],
    }


def test_autosized_heat_pump_curve_meets_load_at_low_flow_fraction():
    result = heat_pump_chiller(
        cooling_load_w=100_000.0,
        source_entering_temp_c=28.0,
        chilled_water_supply_temp_c=12.0,
        source_flow_fraction=0.02,
        load_flow_fraction=0.02,
        config=_Config(),
    )

    assert result["cooling_load_served_w"] == pytest.approx(100_000.0)
    assert result["unmet_cooling_load_w"] == pytest.approx(0.0)


def test_explicit_undersized_heat_pump_still_reports_unmet_load():
    config = _Config()
    config.SEAWATER_HEAT_PUMP_RATED_CAPACITY_W = 50_000.0

    result = heat_pump_chiller(
        cooling_load_w=100_000.0,
        source_entering_temp_c=28.0,
        chilled_water_supply_temp_c=12.0,
        source_flow_fraction=1.0,
        load_flow_fraction=1.0,
        config=config,
    )

    assert result["cooling_load_served_w"] < 100_000.0
    assert result["unmet_cooling_load_w"] > 0.0


class _SeawaterLoopConfig:
    SEAWATER_DENSITY_KG_PER_M3 = 1025.0
    SEAWATER_CP_J_PER_KG_K = 3990.0
    SEAWATER_DELTA_T_C = 5.0
    SEAWATER_MAX_OUTFALL_TEMPERATURE_RISE_C = 3.0
    SEAWATER_MIN_FLOW_M3_S = 0.02
    SEAWATER_MAX_FLOW_M3_S = 3.0
    SEAWATER_MAX_FLOW_PER_UNIT_M3_S = 3.0
    SEAWATER_HEAT_EXCHANGER_UNIT_COUNT = 1
    SEAWATER_AUTO_SIZE_FLOW = True
    SEAWATER_FIXED_PRESSURE_DROP_PA = 250000.0
    SEAWATER_PUMP_EFFICIENCY = 0.80
    SEAWATER_VARIABLE_SPEED_PUMP = True


def test_autosized_source_loop_adds_units_to_meet_outfall_limit():
    heat_rejection_w = 80_000_000.0

    result = seawater_intake_loop(
        heat_rejection_w=heat_rejection_w,
        source_entering_temp_c=20.0,
        config=_SeawaterLoopConfig(),
    )

    assert result["flow_m3_s"] == pytest.approx(result["required_flow_m3_s"])
    assert result["outfall_temperature_rise_c"] <= _SeawaterLoopConfig.SEAWATER_MAX_OUTFALL_TEMPERATURE_RISE_C
    assert result["outfall_temperature_violation"] is False
    assert result["heat_exchange_unit_count"] > 1
    assert result["flow_per_unit_m3_s"] <= _SeawaterLoopConfig.SEAWATER_MAX_FLOW_PER_UNIT_M3_S


def test_fixed_source_loop_reports_violation_when_absolute_flow_cap_is_too_small():
    class FixedFlowConfig(_SeawaterLoopConfig):
        SEAWATER_AUTO_SIZE_FLOW = False

    result = seawater_intake_loop(
        heat_rejection_w=80_000_000.0,
        source_entering_temp_c=20.0,
        config=FixedFlowConfig(),
    )

    assert result["flow_m3_s"] == pytest.approx(FixedFlowConfig.SEAWATER_MAX_FLOW_M3_S)
    assert result["outfall_temperature_violation"] is True
