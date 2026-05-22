# ERA5 offshore-wind input toolkit

This folder stores and regenerates ERA5 meteorological inputs for offshore wind-power modelling at strict-coastal representative sea points.

## Data Source

- Primary meteorological source: ERA5 hourly data from the Copernicus Climate Data Store / ECMWF
- Dataset options used by the script:
  - `reanalysis-era5-single-levels-timeseries`
  - `reanalysis-era5-single-levels` for small-area fallback
- CDS ERA5 page: https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels
- ERA5 documentation: https://confluence.ecmwf.int/display/CKB/ERA5%3A%2Bdata%2Bdocumentation
- Local output files: `OW_*_era5_atmos_2025-01-01_2025-12-31.nc`

ERA5 is a global atmospheric reanalysis with hourly single-level variables. It is used here to create consistent offshore wind-resource inputs for all strict-coastal cities in the project.

## Why This Dataset

The offshore wind analysis needs hourly wind speed, temperature, and pressure at representative sea points near each coastal city. ERA5 is used because it provides globally consistent hourly meteorological variables, including 100 m wind components that are directly relevant to offshore wind turbines.

Typical wind years, monthly wind atlases, or land weather stations are not suitable for this analysis because the optimization model depends on hourly coincidence between wind generation, data-center demand, and grid carbon intensity. Land stations also do not represent offshore wind resources near the selected coastal sea points.

## Files

- `download_era5_strict_coastal_wind_inputs.py`: ERA5 downloader for strict-coastal offshore points.
- `strict_coastal_download_manifest.csv`: point list written by the downloader.
- `strict_coastal_offshore_wind_points_manifest.csv`: audited city-to-sea-point mapping, including selected and nearest ERA5 grid coordinates.
- `request_plan.json`: records the input file, date range, download mode, variable set, and number of points for the current dataset.
- `era5_offshore_wind_required_variables.csv`: variable list and model-use notes.
- `OW_*_era5_atmos_2025-01-01_2025-12-31.nc`: one NetCDF file per offshore point.

At the time of this repository snapshot, the manifest contains 89 strict-coastal offshore representative points.

## Variables

The current `recommended` atmospheric variable set contains:

- `100m_u_component_of_wind`
- `100m_v_component_of_wind`
- `10m_u_component_of_wind`
- `10m_v_component_of_wind`
- `2m_temperature`
- `surface_pressure`
- `2m_dewpoint_temperature`
- `mean_sea_level_pressure`
- `boundary_layer_height`

The wind-generation model primarily uses 100 m and 10 m wind components for wind speed and hub-height extrapolation, plus temperature and pressure for air-density correction.

Optional wave variables can be downloaded with `--include-wave` for future offshore access or availability studies, but they are not required for the current ideal generation model.

## Usage

Configure Copernicus CDS credentials first, then install dependencies:

```bash
pip install cdsapi pandas xarray netCDF4
```

Recommended run from the repository root:

```bash
python data/offshore_wind_download_toolkit/download_era5_strict_coastal_wind_inputs.py ^
  --input data/target_city_map.csv ^
  --output-dir data/offshore_wind_download_toolkit ^
  --start 2025-01-01 ^
  --end 2025-12-31 ^
  --mode timeseries ^
  --variable-set recommended
```

Useful options:

- `--dry-run`: write the manifest and request plan without downloading ERA5 data.
- `--max-points N`: download only the first `N` points for testing.
- `--overwrite`: replace existing NetCDF files.
- `--mode area`: download a small gridded area around each point instead of point time-series.
- `--variable-set core`: download only the minimum wind and density variables.
- `--include-wave`: also download ERA5 wave variables into separate files.

## Point Selection

The downloader reads `data/target_city_map.csv` and selects rows where `Coastal class == "Strict coastal"`. It uses `Representative sea-point latitude` / `Representative sea-point longitude` when available, otherwise backup sea-point coordinates. The output manifest records whether the representative or backup point was used.

## Paper Method Note

A concise methods description can be:

> Offshore wind resources were represented using ERA5 hourly single-level meteorological data at representative sea points for strict-coastal cities. The wind model used ERA5 100 m and 10 m wind components, together with temperature and pressure for air-density correction. ERA5 was selected over land-station or typical-year wind data because the dispatch model requires globally consistent hourly wind generation aligned with 2025 demand and carbon-intensity time series.

When publishing, cite the CDS ERA5 dataset and Copernicus/ECMWF attribution guidance. Also report the representative sea-point selection rule and the ERA5 variables used in the wind model.
