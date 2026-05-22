# ERA5-only EPW generation toolkit

This folder stores and regenerates 2025 Actual Meteorological Year (AMY) EPW weather files for the project city list.

## Data Source

- Primary meteorological source: ERA5 hourly data from the Copernicus Climate Data Store / ECMWF
- Dataset: ERA5 hourly data on single levels from 1940 to present
- CDS page: https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels
- ERA5 documentation: https://confluence.ecmwf.int/display/CKB/ERA5%3A%2Bdata%2Bdocumentation
- EPW format reference: https://climate.onebuilding.org/papers/EnergyPlus_Weather_File_Format.pdf
- Local generated files: `epw_2025_era5_only/*.epw`

The generated EPW files are not typical meteorological year files. They are 2025 AMY files generated from ERA5 hourly reanalysis so that ambient weather, sea-surface temperature, grid carbon intensity, workload, and offshore wind inputs can be aligned to the same study year.

## Why This Dataset

ERA5-only EPW files are used for consistency across all global cities. Earlier EPW workflows often use TMY or CAMS solar-radiation services. TMY files represent a statistically typical year and cannot be directly aligned with 2025 SST, carbon intensity, and wind data. CAMS radiation time-series can fail outside satellite field-of-view regions, which affects many cities in the Americas and other regions.

This toolkit keeps the city coordinates fixed and generates all required weather fields from globally available ERA5 variables. This avoids moving cities to artificial coordinates merely to satisfy a radiation product's coverage limits.

## Files

- `batch_generate_epw_era5_only_global.py`: batch downloader and EPW generator.
- `target_city_map_epw_coordinates_checked.csv`: city coordinates, EPW output coordinates, CAMS field-of-view flags, and target EPW filenames.
- `cams_fov_problem_cities.csv`: records why the project avoids the CAMS-dependent workflow for affected cities.
- `epw_2025_era5_only/`: generated 2025 ERA5 AMY EPW files.
- `epw_2025_era5_only.zip`: archive containing generated EPW files and status metadata.

At the time of this repository snapshot, `epw_2025_era5_only/` contains 209 generated `.epw` files.

## ERA5 Variables

The generator downloads ERA5 single-level variables including:

- `2m_temperature`
- `2m_dewpoint_temperature`
- `surface_pressure`
- `10m_u_component_of_wind`
- `10m_v_component_of_wind`
- `total_cloud_cover`
- `surface_solar_radiation_downwards`
- `total_sky_direct_solar_radiation_at_surface`
- `total_precipitation`
- `snow_depth`

ERA5 radiation accumulations are converted from `J/m2` to `Wh/m2`. Direct normal radiation is estimated from ERA5 direct horizontal radiation and solar zenith angle. This approximation is acceptable for the data-center cooling model because ambient temperature, humidity, and wind dominate the cooling load; it should be separately validated before using these EPW files for high-accuracy solar-energy studies.

## Usage

Install dependencies and configure Copernicus CDS credentials before running:

```bash
pip install cdsapi xarray netCDF4 h5netcdf pandas numpy pvlib timezonefinder tqdm
```

Generate all files:

```bash
python batch_generate_epw_era5_only_global.py ^
  --input target_city_map_epw_coordinates_checked.csv ^
  --year 2025 ^
  --out-dir epw_2025_era5_only ^
  --cache-dir era5_only_cache ^
  --status-csv epw_era5_only_status.csv ^
  --zip-output epw_2025_era5_only.zip
```

Useful options:

- `--limit N`: generate only the first `N` cities for a quick test.
- `--overwrite`: regenerate existing EPW and cache files.
- `--fallback-area`: if the ERA5 time-series request fails, download monthly ERA5 area files around the city.
- `--apply-time-zone-to-data`: shift weather timestamps to standard local time. The project default keeps UTC-like hourly alignment.

## Outputs and Validation

The script writes:

- `epw_2025_era5_only/*.epw`
- `epw_era5_only_status.csv`
- `epw_2025_era5_only.zip`

Validation checks include:

- 8760 hourly rows for non-leap-year 2025.
- no February 29 rows.
- numeric dry-bulb temperature, relative humidity, and pressure fields.
- broad plausibility checks for temperature, humidity, and pressure.

## Paper Method Note

A concise methods description can be:

> City weather files were generated as 2025 Actual Meteorological Year EPW files using ERA5 hourly single-level reanalysis. ERA5 was used instead of TMY weather files to maintain a common 2025 time axis across weather, sea-surface temperature, grid carbon intensity, workload, and offshore wind data. A global ERA5-only radiation workflow was used to avoid CAMS satellite field-of-view failures while preserving the intended city coordinates.

When publishing, cite the CDS ERA5 dataset, ECMWF/Copernicus attribution guidance, and the EnergyPlus EPW file format reference if the EPW conversion process is described.
