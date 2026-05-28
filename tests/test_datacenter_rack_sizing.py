import math

import numpy as np
import pytest

from energy.calculate_datacenter_energy import (
    DEFAULT_TARGET_RACK_IT_POWER_W,
    _build_scaled_dc_config,
    _estimate_detailed_it_power_components_w,
)


def test_medium_facility_uses_weighted_physical_rack_count():
    rated_it_power_kw = 10_108.982314158373

    config = _build_scaled_dc_config(
        rated_it_power_kw=rated_it_power_kw,
        cooling_type="air_source",
        ambient_temperature_c=np.array([35.0]),
        crac_setpoint_c=18.0,
        progress=False,
    )

    expected_physical_racks = math.ceil(rated_it_power_kw * 1000.0 / DEFAULT_TARGET_RACK_IT_POWER_W)
    assert config.PHYSICAL_NUM_RACKS == expected_physical_racks
    assert config.MODELED_NUM_RACKS == config.NUM_RACKS == 20
    assert len(config.RACK_COUNT_MULTIPLIERS) == config.NUM_RACKS
    assert sum(config.RACK_COUNT_MULTIPLIERS) == pytest.approx(expected_physical_racks)
    assert len(config.RACK_SUPPLY_APPROACH_TEMP_LIST) == config.NUM_RACKS
    assert len(config.RACK_RETURN_APPROACH_TEMP_LIST) == config.NUM_RACKS
    assert len(config.RACK_CPU_CONFIG) == config.NUM_RACKS

    modeled_cpu_w, modeled_fan_w = _estimate_detailed_it_power_components_w(
        config,
        cpu_load_fraction=1.0,
        crac_setpoint_c=18.0,
    )
    assert modeled_cpu_w + modeled_fan_w == pytest.approx(rated_it_power_kw * 1000.0, rel=1e-3)
    assert "outlet temperature is higher than 60C" not in "; ".join(
        getattr(config, "_MODEL_WARNING_MESSAGES", [])
    )
