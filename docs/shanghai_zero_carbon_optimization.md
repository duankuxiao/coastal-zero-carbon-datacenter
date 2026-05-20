# Shanghai 8760-Hour Zero-Carbon Dispatch Case

This note summarizes the first deterministic optimization case built on top of
the existing coastal zero-carbon data-center model.

## Case Setup

- City: Shanghai
- Cooling mode: seawater source cooling
- Simulation length: 8760 hours
- Rated IT power: 20000 kW
- Fixed offshore wind capacity: 75.2137 MW
- Wind point: OW_006
- Battery capacity: 535.4 MWh
- Battery round-trip efficiency: 97%
- Grid import limit: 25 MW
- Battery charge limit: 25 MW
- Battery discharge limit: 25 MW
- Flexible load: up to 30% per hour, with annual shifted-down energy limited
  to 30% of annual demand
- Load shifting window: optimistic annual-any-hour movement

The fixed wind capacity is the annual energy-balance capacity from the
seawater baseline result:

```text
required_wind_capacity_mw = 75.21370150314945
```

This capacity makes annual wind generation approximately equal to annual
data-center demand, but it does not guarantee hourly renewable matching.

## Baseline Hourly Mismatch

For Shanghai seawater cooling, the 8760-hour hourly simulation produced:

```text
Annual demand: 163,160.744 MWh
Annual wind at fixed capacity: 163,160.744 MWh
Hourly deficit before storage/load shifting: 66,272.862 MWh
Hourly surplus before storage/load shifting: 66,272.862 MWh
```

This is the core reason storage is required: annual energy balance is not
equivalent to 24/7 physical matching.

## 30% Load Shifting Alone

With the optimistic annual-any-hour load shifting assumption and no battery,
the system cannot reach zero grid purchases:

```text
Feasible without battery: false
Hours where wind < 70% of original demand: 4562
```

The limiting factor is hourly low-wind periods. Even after reducing demand by
30%, many hours still require external supply.

## Ideal Battery Lower Bound

With fixed wind capacity and optimistic annual-any-hour load shifting, the
minimum ideal storage capacity for zero grid purchase was:

```text
Minimum ideal battery capacity: 7,164.143 MWh
```

This assumes perfect dispatch, no charge/discharge power limits, and 100%
round-trip efficiency. It is a theoretical lower bound, not an engineering
design.

## Fixed 535.4 MWh Battery With Engineering Limits

Two objectives were tested under the same engineering limits.

### Minimum Grid Energy

```text
Annual grid purchase: 25,061.930 MWh
Grid-purchase CO2: 9,719.788 tCO2
Average purchased-electricity CI: 387.831 gCO2/kWh
Renewable physical coverage: 84.640%
Wind curtailment: 24,296.072 MWh
Shifted load: 22,975.808 MWh
```

### Minimum Grid CO2

```text
Annual grid purchase: 25,495.045 MWh
Grid-purchase CO2: 8,972.119 tCO2
Average purchased-electricity CI: 351.916 gCO2/kWh
Renewable physical coverage: 84.374%
Wind curtailment: 24,296.072 MWh
Shifted load: 23,695.956 MWh
```

### Difference

Compared with minimum grid energy, the minimum grid CO2 objective:

```text
Bought 433.115 MWh more electricity (+1.728%)
Reduced emissions by 747.669 tCO2 (-7.692%)
Lowered average purchased-electricity CI by 35.915 gCO2/kWh
Did not change wind curtailment under these limits
```

This shows that using hourly carbon-intensity signals changes purchase timing:
the system buys slightly more electricity, but shifts purchases toward lower
carbon-intensity hours.

## Treating Curtailment as Export

The current optimization reports wind curtailment as unused renewable energy.
If the project can export or sell this electricity, model it as:

```text
export_t = curtail_t
revenue_t = export_t * sell_price_t
```

For the Shanghai 535.4 MWh case:

```text
Exportable curtailment: 24,296.072 MWh/year
```

At illustrative fixed sale prices:

```text
100 CNY/MWh -> 2.43 million CNY/year
200 CNY/MWh -> 4.86 million CNY/year
300 CNY/MWh -> 7.29 million CNY/year
400 CNY/MWh -> 9.72 million CNY/year
```

Export revenue improves project economics. It should be reported separately
from the data center's own physical 24/7 renewable coverage unless a system
boundary expansion or avoided-emissions accounting method is explicitly used.

## Running The Optimizer

The reusable optimizer is in:

```text
core/optimize_zero_carbon.py
```

Example CLI:

```bash
python -m core.optimize_zero_carbon \
  --city Shanghai \
  --cooling seawater \
  --wind-capacity-mw 75.21370150314945 \
  --wind-nc-file data/offshore_wind_download_toolkit/OW_006_China_Shanghai_era5_atmos_2025-01-01_2025-12-31.nc \
  --battery-capacity-mwh 535.4 \
  --battery-roundtrip-efficiency 0.97 \
  --grid-import-limit-mw 25 \
  --battery-charge-limit-mw 25 \
  --battery-discharge-limit-mw 25 \
  --load-shift-fraction 0.3 \
  --objective min-grid-co2
```

Use `--objective min-grid-mwh` to minimize electricity purchased from the grid
instead of grid-purchase emissions.
