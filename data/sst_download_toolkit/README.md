# Sea-surface temperature collection toolkit

This folder stores and regenerates hourly sea-surface temperature (SST) inputs for non-inland cities.

## Data Source

- Provider: Open-Meteo Marine API
- API documentation: https://open-meteo.com/en/docs/marine-weather-api
- Variable: `sea_surface_temperature`
- Unit: degrees Celsius (`degC`)
- Time zone: UTC
- Local output file: `sea_surface_temperature_2025_openmeteo.csv`

The Open-Meteo Marine API aggregates marine model products behind a simple coordinate-based API. For this project, the script queries representative sea-point coordinates from `data/target_city_map.csv` and writes a wide hourly SST table.

## Why This Dataset

Sea-surface temperature is required for the seawater cooling model because the relevant heat sink is seawater near the data-center coastal location, not inland air temperature. Using land air temperature or typical monthly seawater climatology would remove the hourly thermal signal needed to align cooling performance with workload, grid carbon intensity, and renewable dispatch.

Open-Meteo is used because it provides accessible global marine time series through a reproducible API. The project keeps the requested coordinates explicit in the city map so that the data source can be audited or replaced by another marine dataset if a later paper version requires a different license or resolution.

## Files

- `collect_sst_openmeteo_quick.py`: SST downloader.
- `sea_surface_temperature_2025_openmeteo.csv`: wide hourly SST table.
- `SST_collection_README.md`: older collection note retained for provenance.

At the time of this repository snapshot, `sea_surface_temperature_2025_openmeteo.csv` contains 8760 hourly records plus a header row. It has one `timestamp` column and 125 city columns.

## Target Selection

The script reads `data/target_city_map.csv` by default and selects rows where:

- `Coastal class != Inland`
- `Representative sea-point latitude` and `Representative sea-point longitude` are available

If a city column is fully missing after the main download, the script can retry using backup sea-point coordinates when they are available.

## Usage

Run from the repository root:

```bash
python data/sst_download_toolkit/collect_sst_openmeteo_quick.py ^
  --input data/target_city_map.csv ^
  --year 2025 ^
  --output data/sst_download_toolkit/sea_surface_temperature_2025_openmeteo.csv
```

Useful options:

- `--chunk-size N`: number of city coordinates per API call.
- `--save-targets`: write the resolved sea-point target list.
- `--save-template`: write a blank 8760-row output template.
- `--repair-missing` / `--no-repair-missing`: retry fully missing columns using backup coordinates.
- `--fill-missing` / `--no-fill-missing`: fill internal missing gaps by linear interpolation.
- `--dry-run`: resolve targets without downloading SST values.

## Output Format

```text
timestamp,Shanghai,Los Angeles,...
2025-01-01 00:00,7.3,14.9,...
```

The timestamp column is hourly and uses UTC-style timestamps. Downstream energy-model code aligns SST with workload, EPW weather, and carbon-intensity inputs.

## Paper Method Note

A concise methods description can be:

> Hourly sea-surface temperature was collected from the Open-Meteo Marine API for representative coastal sea points associated with non-inland data-center cities. SST was used as the seawater cooling source temperature because it directly represents the marine heat sink, whereas land weather files only provide ambient air conditions.

When publishing, cite Open-Meteo Marine API documentation and clearly state the selected variable, time zone, year, and representative sea-point coordinate rule.
