import numpy as np
import pytest

from core.optimize_zero_carbon import (
    OptimizationInputs,
    optimize_dispatch,
)


def test_min_grid_mwh_uses_storage_and_reports_curtailment():
    inputs = OptimizationInputs(
        demand_mwh=np.array([10.0, 10.0, 10.0, 10.0]),
        wind_mwh=np.array([20.0, 0.0, 20.0, 0.0]),
        carbon_intensity_g_per_kwh=np.array([500.0, 500.0, 500.0, 500.0]),
        battery_capacity_mwh=5.0,
        battery_roundtrip_efficiency=1.0,
        grid_import_limit_mw=20.0,
        battery_charge_limit_mw=20.0,
        battery_discharge_limit_mw=20.0,
        load_shift_fraction=0.0,
    )

    result = optimize_dispatch(inputs, objective="min-grid-mwh")

    assert result.grid_purchase_mwh == pytest.approx(10.0)
    assert result.wind_curtailment_mwh == pytest.approx(10.0)
    assert result.battery_discharge_mwh == pytest.approx(10.0)
    assert result.battery_conversion_loss_mwh == pytest.approx(0.0)


def test_min_grid_co2_prefers_low_carbon_purchase_with_same_engineering_limits():
    inputs = OptimizationInputs(
        demand_mwh=np.array([10.0, 10.0]),
        wind_mwh=np.array([0.0, 0.0]),
        carbon_intensity_g_per_kwh=np.array([100.0, 1000.0]),
        battery_capacity_mwh=10.0,
        battery_roundtrip_efficiency=0.81,
        grid_import_limit_mw=20.0,
        battery_charge_limit_mw=20.0,
        battery_discharge_limit_mw=20.0,
        load_shift_fraction=0.0,
    )

    min_mwh = optimize_dispatch(inputs, objective="min-grid-mwh")
    min_co2 = optimize_dispatch(inputs, objective="min-grid-co2")

    assert min_co2.grid_purchase_mwh >= min_mwh.grid_purchase_mwh
    assert min_co2.grid_purchase_co2_kg < min_mwh.grid_purchase_co2_kg
    assert min_co2.average_grid_carbon_intensity_g_per_kwh < min_mwh.average_grid_carbon_intensity_g_per_kwh


def test_load_shift_budget_and_hourly_bounds_are_enforced():
    inputs = OptimizationInputs(
        demand_mwh=np.array([10.0, 10.0, 10.0, 10.0]),
        wind_mwh=np.array([13.0, 7.0, 13.0, 7.0]),
        carbon_intensity_g_per_kwh=np.array([400.0, 400.0, 400.0, 400.0]),
        battery_capacity_mwh=0.0,
        battery_roundtrip_efficiency=1.0,
        grid_import_limit_mw=20.0,
        battery_charge_limit_mw=0.0,
        battery_discharge_limit_mw=0.0,
        load_shift_fraction=0.3,
    )

    result = optimize_dispatch(inputs, objective="min-grid-mwh")

    assert result.grid_purchase_mwh == pytest.approx(0.0)
    assert result.shifted_down_mwh == pytest.approx(6.0)
    assert result.shifted_down_mwh <= 0.3 * inputs.demand_mwh.sum()
    assert np.all(result.optimized_demand_mwh >= 0.7 * inputs.demand_mwh - 1e-8)
    assert np.all(result.optimized_demand_mwh <= 1.3 * inputs.demand_mwh + 1e-8)
