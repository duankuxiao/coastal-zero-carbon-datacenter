"""
Download ERA5 hourly meteorological inputs for offshore wind power modelling
from the City_manifest sheet in data/coastal_datacenter_city_manifest.xlsx.

Windows example:
    pip install cdsapi pandas xarray netcdf4
    python download_era5_strict_coastal_wind_inputs.py ^
        --input ../coastal_datacenter_city_manifest.xlsx ^
        --sheet City_manifest ^
        --output-dir . ^
        --start 2024-01-01 --end 2024-12-31 ^
        --mode timeseries --variable-set recommended

CDS credentials:
    Configure CDS API first. Current CDS normally uses a Personal Access Token.
    See the CDS website profile/API page and accept the ERA5 dataset licence.

Notes:
    1) mode=timeseries uses the CDS point time-series dataset:
       reanalysis-era5-single-levels-timeseries
       It is efficient for many point locations.
    2) mode=area uses the standard ERA5 single-level dataset:
       reanalysis-era5-single-levels
       It downloads a small box around the requested point, split by year.
    3) If CDS changes parameter names, open the CDS dataset page, select the
       same variables, click "Show API request code", and adjust the request
       dictionaries in retrieve_timeseries() or retrieve_area_year().
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import cdsapi
except ImportError as exc:
    raise SystemExit("Missing dependency: cdsapi. Install with: pip install cdsapi") from exc

CORE_VARIABLES = [
    "100m_u_component_of_wind",
    "100m_v_component_of_wind",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "2m_temperature",
    "surface_pressure",
]

RECOMMENDED_EXTRA_VARIABLES = [
    "2m_dewpoint_temperature",
    "mean_sea_level_pressure",
    "boundary_layer_height",
]

OPTIONAL_EXTRA_VARIABLES = [
    "sea_surface_temperature",
]

WAVE_VARIABLES = [
    "significant_height_of_combined_wind_waves_and_swell",
    "mean_wave_period",
    "mean_wave_direction",
]

XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
XLSX_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
MANIFEST_SHEET_NAME = "City_manifest"


def clean_header(value: object) -> str:
    return str(value).replace("\ufeff", "").strip()


def xlsx_col_index(cell_ref: str) -> int:
    match = re.match(r"([A-Z]+)", cell_ref)
    if not match:
        return 0
    n = 0
    for ch in match.group(1):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx_sheet_rows(path: Path, sheet_name: str) -> List[Dict[str, Any]]:
    """Read a plain worksheet from xlsx using only the standard library."""
    with zipfile.ZipFile(path) as z:
        shared: List[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(XLSX_NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(XLSX_NS + "t")))

        workbook = ET.fromstring(z.read("xl/workbook.xml"))
        sheets = workbook.find(XLSX_NS + "sheets")
        rid = None
        for sheet in [] if sheets is None else sheets:
            if sheet.attrib.get("name") == sheet_name:
                rid = sheet.attrib[XLSX_REL_ID]
                break
        if rid is None:
            raise ValueError(f"Sheet not found: {sheet_name}")

        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = None
        for rel in rels:
            if rel.attrib.get("Id") == rid:
                target = rel.attrib["Target"].lstrip("/")
                break
        if target is None:
            raise ValueError(f"Worksheet relationship not found for sheet: {sheet_name}")
        if not target.startswith("xl/"):
            target = "xl/" + target

        sheet = ET.fromstring(z.read(target))
        rows: List[List[Any]] = []
        for row in sheet.findall(XLSX_NS + "sheetData/" + XLSX_NS + "row"):
            cells: Dict[int, Any] = {}
            max_col = -1
            for cell in row.findall(XLSX_NS + "c"):
                col_idx = xlsx_col_index(cell.attrib.get("r", "A1"))
                cell_type = cell.attrib.get("t")
                value_node = cell.find(XLSX_NS + "v")
                value: Any = None
                if cell_type == "inlineStr":
                    value = "".join(t.text or "" for t in cell.iter(XLSX_NS + "t"))
                elif value_node is not None:
                    raw = value_node.text
                    if cell_type == "s":
                        value = shared[int(raw)]
                    elif cell_type == "b":
                        value = bool(int(raw))
                    else:
                        try:
                            value = float(raw)
                            if value.is_integer():
                                value = int(value)
                        except Exception:
                            value = raw
                cells[col_idx] = value
                max_col = max(max_col, col_idx)
            rows.append([cells.get(i) for i in range(max_col + 1)])

    if not rows:
        return []
    header = [clean_header(v) for v in rows[0]]
    out: List[Dict[str, Any]] = []
    for row in rows[1:]:
        out.append({
            header[i]: row[i] if i < len(row) else None
            for i in range(len(header))
            if header[i] not in {"", "None"}
        })
    return out


def is_toolkit_ready(row: Dict[str, Any]) -> bool:
    return str(row.get("toolkit_ready", "")).strip().lower() in {"1", "true", "yes", "y"}


def required_text(row: Dict[str, Any], column: str, source_row: int) -> str:
    value = row.get(column)
    text = "" if value is None else str(value).strip()
    if not text:
        raise ValueError(f"Missing required column value '{column}' at workbook row {source_row}")
    return text


def make_point_id(value: object, fallback: int) -> str:
    try:
        return f"OW_{int(float(str(value).strip())):03d}"
    except Exception:
        return f"OW_{fallback:03d}"


def parse_float(x: object) -> Optional[float]:
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() in {"nan", "none", "null"}:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if not math.isfinite(v):
        return None
    return v


def valid_coord(lat: Optional[float], lon: Optional[float]) -> bool:
    return lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180


def choose_offshore_wind_point(row: Dict[str, Any]) -> Tuple[float, float, str]:
    lat = parse_float(row.get("offshore_wind_lat"))
    lon = parse_float(row.get("offshore_wind_lon"))
    if valid_coord(lat, lon):
        return float(lat), float(lon), "offshore_wind"
    raise ValueError("No valid offshore_wind_lat/offshore_wind_lon coordinate")


def safe_name(text: str, max_len: int = 80) -> str:
    text = re.sub(r"[^A-Za-z0-9_\-\.]+", "_", text.strip())
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:max_len] or "unnamed"


def iter_offshore_wind_points(workbook_path: Path, sheet_name: str) -> List[Dict[str, object]]:
    rows = read_xlsx_sheet_rows(workbook_path, sheet_name)
    if not rows:
        raise ValueError(f"Manifest is empty: {workbook_path}")
    required_columns = [
        "repo_city_index",
        "country",
        "datacentermap_market",
        "toolkit_ready",
        "offshore_wind_lat",
        "offshore_wind_lon",
    ]
    missing = [c for c in required_columns if c not in rows[0]]
    if missing:
        raise ValueError(f"Workbook sheet {sheet_name} is missing required columns: {', '.join(missing)}")

    points: List[Dict[str, object]] = []
    for line_no, row in enumerate(rows, start=2):
        if not is_toolkit_ready(row):
            continue
        try:
            lat, lon, coord_source = choose_offshore_wind_point(row)
        except ValueError as exc:
            print(f"[SKIP] row={line_no}: {exc}", file=sys.stderr)
            continue
        points.append(
            {
                "point_id": make_point_id(row.get("repo_city_index"), len(points) + 1),
                "source_row": line_no,
                "country_area": required_text(row, "country", line_no),
                "region": "",
                "city_metro": required_text(row, "datacentermap_market", line_no),
                "lat": lat,
                "lon": lon,
                "coordinate_source": coord_source,
            }
            )
    return points


def write_manifest(points: List[Dict[str, object]], path: Path) -> None:
    fieldnames = [
        "point_id",
        "source_row",
        "country_area",
        "region",
        "city_metro",
        "lat",
        "lon",
        "coordinate_source",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(points)


def year_chunks(start: dt.date, end: dt.date) -> Iterable[Tuple[int, dt.date, dt.date]]:
    current = start
    while current <= end:
        chunk_end = min(dt.date(current.year, 12, 31), end)
        yield current.year, current, chunk_end
        current = chunk_end + dt.timedelta(days=1)


def days_between(start: dt.date, end: dt.date) -> List[str]:
    n = (end - start).days + 1
    return sorted({(start + dt.timedelta(days=i)).strftime("%d") for i in range(n)})


def months_between(start: dt.date, end: dt.date) -> List[str]:
    vals = set()
    cur = dt.date(start.year, start.month, 1)
    while cur <= end:
        vals.add(f"{cur.month:02d}")
        if cur.month == 12:
            cur = dt.date(cur.year + 1, 1, 1)
        else:
            cur = dt.date(cur.year, cur.month + 1, 1)
    return sorted(vals)


def retrieve_timeseries(
    client: "cdsapi.Client",
    variables: List[str],
    lat: float,
    lon: float,
    start: dt.date,
    end: dt.date,
    target: Path,
    data_format: str = "netcdf",
) -> None:
    request = {
        "variable": variables,
        "location": {"latitude": lat, "longitude": lon},
        "date": [f"{start.isoformat()}/{end.isoformat()}"],
        "data_format": data_format,
    }
    client.retrieve("reanalysis-era5-single-levels-timeseries", request, str(target))


def retrieve_area_year(
    client: "cdsapi.Client",
    variables: List[str],
    lat: float,
    lon: float,
    year: int,
    start: dt.date,
    end: dt.date,
    target: Path,
    data_format: str = "netcdf",
    buffer_deg: float = 0.125,
) -> None:
    north = min(90, lat + buffer_deg)
    south = max(-90, lat - buffer_deg)
    west = max(-180, lon - buffer_deg)
    east = min(180, lon + buffer_deg)
    request = {
        "product_type": ["reanalysis"],
        "variable": variables,
        "year": [str(year)],
        "month": months_between(start, end),
        "day": days_between(start, end),
        "time": [f"{h:02d}:00" for h in range(24)],
        "data_format": data_format,
        "download_format": "unarchived",
        "area": [north, west, south, east],
    }
    client.retrieve("reanalysis-era5-single-levels", request, str(target))


def variable_list(name: str, include_wave: bool) -> Tuple[List[str], List[str]]:
    if name == "core":
        atm = list(CORE_VARIABLES)
    elif name == "recommended":
        atm = list(CORE_VARIABLES) + list(RECOMMENDED_EXTRA_VARIABLES)
    elif name == "all":
        atm = list(CORE_VARIABLES) + list(RECOMMENDED_EXTRA_VARIABLES) + list(OPTIONAL_EXTRA_VARIABLES)
    else:
        raise ValueError(f"Unknown variable set: {name}")
    wave = list(WAVE_VARIABLES) if include_wave else []
    return atm, wave

ROOT_DIR = Path(__file__).resolve().parent.parent
CITY_MANIFEST_FILE = ROOT_DIR / "coastal_datacenter_city_manifest.xlsx"

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=CITY_MANIFEST_FILE, help="Input city manifest XLSX")
    parser.add_argument("--sheet", default=MANIFEST_SHEET_NAME, help="Workbook sheet containing city targets")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent, help="Directory for downloaded files")
    parser.add_argument("--start", default="2025-01-01", help="Start date, e.g. 2025-01-01")
    parser.add_argument("--end", default="2025-12-31", help="End date, e.g. 2025-12-31")
    parser.add_argument("--mode", choices=["timeseries", "area"], default="timeseries")
    parser.add_argument("--variable-set", choices=["core", "recommended", "all"], default="recommended")
    parser.add_argument("--include-wave", action="store_true", help="Also download ERA5 wave variables into separate files")
    parser.add_argument("--data-format", choices=["netcdf", "csv"], default="netcdf")
    parser.add_argument("--max-points", type=int, default=None, help="Debug: process first N points only")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Only write manifest and planned requests; do not call CDS")
    args = parser.parse_args()

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    if end < start:
        raise SystemExit("--end must be >= --start")

    points = iter_offshore_wind_points(args.input, args.sheet)
    if args.max_points is not None:
        points = points[: args.max_points]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    wind_data_dir = args.output_dir / "offshore_wind"
    wind_data_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "strict_coastal_download_manifest.csv"
    write_manifest(points, manifest_path)

    atm_vars, wave_vars = variable_list(args.variable_set, args.include_wave)
    plan = {
        "input": str(args.input),
        "start": args.start,
        "end": args.end,
        "mode": args.mode,
        "variable_set": args.variable_set,
        "atmospheric_variables": atm_vars,
        "wave_variables": wave_vars,
        "n_points": len(points),
        "manifest": str(manifest_path),
        "wind_data_dir": str(wind_data_dir),
    }
    (args.output_dir / "request_plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(plan, indent=2, ensure_ascii=False))

    if args.dry_run:
        print("Dry run only; no CDS requests submitted.")
        return

    client = cdsapi.Client()
    suffix = "nc" if args.data_format == "netcdf" else "csv"

    for point in points:
        point_name = safe_name(f"{point['point_id']}_{point['country_area']}_{point['city_metro']}")
        lat = float(point["lat"])
        lon = float(point["lon"])
        print(f"\n[POINT] {point_name} lat={lat} lon={lon}")

        if args.mode == "timeseries":
            target = wind_data_dir / f"{point_name}_era5_atmos_{args.start}_{args.end}.{suffix}"
            if target.exists() and not args.overwrite:
                print(f"[SKIP existing] {target}")
            else:
                print(f"[DOWNLOAD] {target}")
                retrieve_timeseries(client, atm_vars, lat, lon, start, end, target, args.data_format)

            if wave_vars:
                target_wave = wind_data_dir / f"{point_name}_era5_wave_{args.start}_{args.end}.{suffix}"
                if target_wave.exists() and not args.overwrite:
                    print(f"[SKIP existing] {target_wave}")
                else:
                    print(f"[DOWNLOAD] {target_wave}")
                    retrieve_timeseries(client, wave_vars, lat, lon, start, end, target_wave, args.data_format)

        else:
            for year, chunk_start, chunk_end in year_chunks(start, end):
                target = wind_data_dir / f"{point_name}_era5_atmos_{year}.{suffix}"
                if target.exists() and not args.overwrite:
                    print(f"[SKIP existing] {target}")
                else:
                    print(f"[DOWNLOAD] {target}")
                    retrieve_area_year(client, atm_vars, lat, lon, year, chunk_start, chunk_end, target, args.data_format)
                if wave_vars:
                    target_wave = wind_data_dir / f"{point_name}_era5_wave_{year}.{suffix}"
                    if target_wave.exists() and not args.overwrite:
                        print(f"[SKIP existing] {target_wave}")
                    else:
                        print(f"[DOWNLOAD] {target_wave}")
                        retrieve_area_year(client, wave_vars, lat, lon, year, chunk_start, chunk_end, target_wave, args.data_format)


if __name__ == "__main__":
    main()
