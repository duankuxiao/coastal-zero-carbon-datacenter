# Electricity Maps carbon-intensity data

This folder stores and regenerates hourly grid carbon-intensity inputs used by the coastal data-center simulations.

## Data Source

- Provider: Electricity Maps API
- API endpoint used by the script: `/v3/carbon-intensity/past-range`
- Documentation: https://portal.electricitymaps.com/docs/api
- Unit: `gCO2eq/kWh`
- Time zone: UTC
- Local output file: `carbon_intensity_electricitymaps.csv`

Electricity Maps reports carbon intensity as emissions per unit of electricity consumed on the grid. The API supports both `direct` operational emission factors and `lifecycle` emission factors. This project uses `direct` by default because the optimization model evaluates operational grid-purchase emissions during hourly dispatch. Use `lifecycle` only if the paper's accounting boundary includes infrastructure and fuel life-cycle emissions.

## Why This Dataset

Electricity Maps is used because it provides hourly, geographically resolved grid carbon intensity and supports coordinate-to-zone lookup. This is important for data-center dispatch analysis because carbon emissions depend not only on annual electricity use but also on the timing and location of grid purchases.

Alternatives such as annual country-average emission factors are easier to cite but are not suitable for this project's hourly load-shifting and storage optimization. They cannot represent intra-day or seasonal variation in grid emissions and therefore cannot evaluate carbon-aware dispatch.

## Files

- `carbon_intensity_electricitymaps.csv`: wide hourly table. The first column is `timestamp`; each following column is a city or metro area from the project city list.
- `download_electricitymaps_10y_hourly.py`: downloader for Electricity Maps historical hourly carbon intensity.

Optional files produced when running with `--save-logs`:

- `city_zone_resolution.csv`: coordinate-to-zone resolution and fallback/skipped reasons.
- `city_missingness_summary.csv`: coverage ratio and missing points by city.
- `download_log.csv`: API request status by target and time chunk.
- `available_zones.json`: Electricity Maps zones available to the API token.
- `run_metadata.json`: data range, endpoint, emission factor type, and output metadata.

## Usage

Set an API token first:

```bash
set ELECTRICITYMAPS_TOKEN=your_token
```

Recommended run for operational emissions:

```bash
python download_electricitymaps_10y_hourly.py ^
  --manifest city_electricitymaps_request_manifest.csv ^
  --output-dir . ^
  --output-wide carbon_intensity_electricitymaps.csv ^
  --end now ^
  --years-back 10 ^
  --emission-factor-type direct ^
  --unavailable-zone-action missing ^
  --save-logs
```

If a resolved Electricity Maps zone is unavailable, the recommended default is `--unavailable-zone-action missing`. A proxy zone should only be used with an explicit `--zone-fallback-map` and should be documented in the paper.

## Output Format

```text
timestamp,Shanghai,Los Angeles,...
2017/01/01 0:00,574,204,...
```

The simulation code reads this wide table and aligns it to the workload, EPW weather, and SST time axis. Missing short gaps can be interpolated by downstream code according to the configured `max_carbon_gap_hours`.

## Paper Method Note

A concise methods description can be:

> Hourly grid carbon intensity was obtained from the Electricity Maps carbon-intensity API and stored as city-level wide time series in UTC. Direct operational emission factors were used to match the operational dispatch boundary of the data-center model. City coordinates were resolved to Electricity Maps zones, and unsupported zones were left missing unless an explicit documented fallback was provided.

When publishing, cite Electricity Maps API documentation and state whether `direct` or `lifecycle` emission factors were used.
