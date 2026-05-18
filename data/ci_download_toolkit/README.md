# Electricity Maps 10-year hourly downloader, v2

This version is designed to avoid repeated failures such as:

```text
HTTP 400: {"status":"error","message":"Zone 'CL-SEM' does not exist."}
```

This happens when a coordinate resolves to a geographic/electrical zone that is not exposed by the `carbon-intensity/past-range` endpoint for your token or for historical carbon-intensity coverage. The script now:

1. Resolves coordinates to a zone using `/v3/zone`, then falls back to `/v3/carbon-intensity/latest`.
2. Reads available zones from `/v3/zones`.
3. If the resolved zone is unavailable, defaults to `missing`, i.e. leaves that city's output blank and logs the reason.
4. Supports explicit proxy mapping via `--zone-fallback-map`, but does not apply proxy mappings silently.

## Recommended conservative run

```bash
export ELECTRICITYMAPS_TOKEN="your_token"

python download_electricitymaps_10y_hourly.py \
  --manifest city_electricitymaps_request_manifest.csv \
  --output-dir ci_download_toolkit \
  --output-wide city_grid_carbon_intensity_electricitymaps_10y.csv \
  --end now \
  --years-back 10 \
  --emission-factor-type direct \
  --unavailable-zone-action missing
```

## Proxy fallback run

Use this only if you accept a documented proxy assumption for unsupported zones.

```bash
python download_electricitymaps_10y_hourly.py \
  --manifest city_electricitymaps_request_manifest.csv \
  --output-dir ci_download_toolkit \
  --output-wide carbon_intensity_electricitymaps.csv \
  --end now \
  --years-back 10 \
  --emission-factor-type direct \
  --zone-fallback-map zone_fallback_map.example.csv \
  --unavailable-zone-action fallback_map
```

## Outputs

- `city_grid_carbon_intensity_electricitymaps_10y.csv`: wide table in `timestamp, city1, city2, ...` format.
- `city_zone_resolution.csv`: initial zone, final zone, fallback/skipped reason.
- `city_missingness_summary.csv`: coverage ratio per city.
- `download_log.csv`: API request status by target and time chunk.
- `available_zones.json`: zones returned by `/v3/zones` for your token.

## Notes

- Default unit is `gCO2eq/kWh`.
- Timestamps are UTC.
- `direct` emission factors are usually preferable for operational emissions and dispatch strategy. Use `lifecycle` if your accounting boundary requires lifecycle emissions.
- Hourly `past-range` calls are chunked into 10-day windows by default.
