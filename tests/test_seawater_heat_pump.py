import pytest

from energy.seawater_heat_pump import heat_pump_chiller


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
