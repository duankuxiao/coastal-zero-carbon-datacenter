#!/usr/bin/env python3
"""
Collect hourly sea_surface_temperature for non-Inland cities in sheet
Country_city_map of the input workbook.

Output format:
  timestamp,<City / metro 1>,<City / metro 2>,...
  2023-01-01 00:00,<degC>,...

Data source: Open-Meteo Marine API, variable sea_surface_temperature.
Default year is 2023, a non-leap year with 8760 hourly timestamps.

This script uses only Python standard libraries.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Tuple, Any

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
SCRIPT_DIR = Path(__file__).resolve().parent


def format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def print_progress(enabled: bool, message: str) -> None:
    if enabled:
        print(f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def estimate_remaining(start_time: float, completed: int, total: int) -> str:
    if completed <= 0 or total <= 0:
        return "unknown"
    elapsed = time.time() - start_time
    remaining = elapsed * max(0, total - completed) / completed
    return format_elapsed(remaining)


def resolve_local_path(path: str) -> str:
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str(SCRIPT_DIR / p)


def xlsx_col_index(cell_ref: str) -> int:
    letters = re.match(r"([A-Z]+)", cell_ref).group(1)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def write_wide_csv_atomic(data: Dict[str, List[Any]], output: str) -> None:
    tmp = output + ".tmp"
    write_wide_csv(data, tmp)
    os.replace(tmp, output)

def read_xlsx_sheet_rows(path: str, sheet_name: str) -> List[Dict[str, Any]]:
    """Read a simple worksheet from xlsx using the standard library."""
    with zipfile.ZipFile(path) as z:
        # Shared strings
        shared: List[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(NS + "t")))

        # Workbook sheet name -> rel id
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        rid = None
        for s in wb.find(NS + "sheets"):
            if s.attrib.get("name") == sheet_name:
                rid = s.attrib[rel_ns]
                break
        if rid is None:
            raise ValueError(f"Sheet not found: {sheet_name}")

        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = None
        for rel in rels:
            if rel.attrib.get("Id") == rid:
                target = rel.attrib["Target"]
                break
        if target is None:
            raise ValueError(f"Worksheet relationship not found for sheet: {sheet_name}")
        if not target.startswith("worksheets/"):
            target = "worksheets/" + target

        sheet = ET.fromstring(z.read("xl/" + target))
        rows: List[List[Any]] = []
        for row in sheet.findall(NS + "sheetData/" + NS + "row"):
            cells: Dict[int, Any] = {}
            maxc = -1
            for c in row.findall(NS + "c"):
                ci = xlsx_col_index(c.attrib["r"])
                cell_type = c.attrib.get("t")
                v = c.find(NS + "v")
                val: Any = None
                if v is not None:
                    raw = v.text
                    if cell_type == "s":
                        val = shared[int(raw)]
                    elif cell_type == "b":
                        val = bool(int(raw))
                    else:
                        try:
                            val = float(raw)
                            if val.is_integer():
                                val = int(val)
                        except Exception:
                            val = raw
                cells[ci] = val
                maxc = max(maxc, ci)
            rows.append([cells.get(i) for i in range(maxc + 1)])

    if not rows:
        return []
    header = rows[0]
    out: List[Dict[str, Any]] = []
    for r in rows[1:]:
        out.append({header[i]: r[i] if i < len(r) else None for i in range(len(header))})
    return out


def clean_header(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\ufeff", "").strip()
    return value


def read_csv_rows(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [
            {clean_header(k): v for k, v in row.items()}
            for row in reader
        ]


def load_targets(input_xlsx: str, sheet_name: str) -> List[Dict[str, Any]]:
    input_path = str(input_xlsx)
    if input_path.lower().endswith(".csv"):
        rows = read_csv_rows(input_path)
    else:
        rows = read_xlsx_sheet_rows(input_path, sheet_name)
    targets = []
    for r in rows:
        if r.get("Coastal class") == "Inland":
            continue
        lat = r.get("Representative sea-point latitude")
        lon = r.get("Representative sea-point longitude")
        if lat is None or lon is None:
            continue
        city = str(r.get("City / metro")).strip()
        targets.append({
            "country_area": r.get("Country/Area"),
            "region": r.get("Region"),
            "city_name": city,
            "coastal_class": r.get("Coastal class"),
            "sea_latitude": float(lat),
            "sea_longitude": float(lon),
            "backup_sea_latitude": r.get("Backup sea-point latitude"),
            "backup_sea_longitude": r.get("Backup sea-point longitude"),
        })
    # Protect against duplicate CSV column names; input currently has unique City / metro names.
    seen = set()
    for t in targets:
        name = t["city_name"]
        if name in seen:
            t["city_name"] = f"{name} [{t['country_area']}]"
        seen.add(t["city_name"])
    return targets


def expected_timestamps(year: int) -> List[str]:
    if (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0):
        raise ValueError(f"{year} is a leap year; choose a non-leap year for exactly 8760 hours.")
    start = dt.datetime(year, 1, 1, 0, 0)
    return [(start + dt.timedelta(hours=i)).strftime("%Y-%m-%d %H:%M") for i in range(8760)]


def fetch_chunk(targets: List[Dict[str, Any]], year: int, timeout: int, models: str | None = None) -> List[Dict[str, Any]]:
    lats = ",".join(str(t["sea_latitude"]) for t in targets)
    lons = ",".join(str(t["sea_longitude"]) for t in targets)
    params = {
        "latitude": lats,
        "longitude": lons,
        "hourly": "sea_surface_temperature",
        "start_date": f"{year}-01-01",
        "end_date": f"{year}-12-31",
        "timezone": "UTC",
        "timeformat": "iso8601",
        "cell_selection": "sea",
    }
    if models:
        params["models"] = models
    url = "https://marine-api.open-meteo.com/v1/marine?" + urllib.parse.urlencode(params, safe=",")
    req = urllib.request.Request(url, headers={"User-Agent": "coastal-sst-collector/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(payload.get("reason", "Open-Meteo API returned an error"))
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected Open-Meteo response structure")
    return payload


def retry_wait_seconds(exc: Exception, attempt: int) -> float:
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
        retry_after = exc.headers.get("Retry-After")
        if retry_after:
            try:
                return min(900.0, max(5.0, float(retry_after)))
            except ValueError:
                pass
        return min(900.0, 30.0 * (2 ** attempt))
    return min(60.0, 2.0 * (2 ** attempt))


def normalize_times(time_values: Iterable[str]) -> List[str]:
    # API returns e.g. 2023-01-01T00:00. Convert to required CSV index format.
    return [str(t).replace("T", " ") for t in time_values]


def is_missing_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"", "nan", "none", "null"}
    return False


def count_missing_values(values: Iterable[Any]) -> int:
    return sum(1 for value in values if is_missing_value(value))


def values_from_payload(target: Dict[str, Any], payload: Dict[str, Any], expected: List[str]) -> List[Any]:
    hourly = payload.get("hourly", {})
    times = normalize_times(hourly.get("time", []))
    values = hourly.get("sea_surface_temperature", [])
    if len(times) != 8760 or len(values) != 8760:
        raise RuntimeError(
            f"{target['city_name']} returned {len(values)} values / {len(times)} timestamps, expected 8760."
        )
    if times != expected:
        # Align by timestamp to guard against timezone or missing-step issues.
        mapper = dict(zip(times, values))
        missing = [t for t in expected if t not in mapper]
        if missing:
            raise RuntimeError(f"{target['city_name']} missing timestamps, first missing: {missing[0]}")
        values = [mapper[t] for t in expected]
    return values


def collect_sst(
    targets: List[Dict[str, Any]],
    year: int,
    chunk_size: int,
    timeout: int,
    pause: float,
    retries: int,
    models: str | None,
    progress: bool,
    output: str | None = None,
    existing_data: Dict[str, List[Any]] | None = None,
) -> Dict[str, List[Any]]:
    expected = expected_timestamps(year)

    if existing_data is not None:
        data = existing_data
        if data.get("timestamp") != expected:
            raise RuntimeError("Existing output timestamp column does not match expected timestamps.")
    else:
        data: Dict[str, List[Any]] = {"timestamp": expected}

    total_chunks = (len(targets) + chunk_size - 1) // chunk_size
    started = time.time()

    for i in range(0, len(targets), chunk_size):
        chunk_no = i // chunk_size + 1

        chunk = [
            target for target in targets[i:i + chunk_size]
            if target["city_name"] not in data
            or count_missing_values(data[target["city_name"]]) == len(data["timestamp"])
        ]
        if not chunk:
            print_progress(progress, f"Skipping chunk {chunk_no}/{total_chunks}: already downloaded")
            continue

        first_city = chunk[0]["city_name"] if chunk else ""
        last_city = chunk[-1]["city_name"] if chunk else ""
        print_progress(
            progress,
            f"Main download chunk {chunk_no}/{total_chunks}: {len(chunk)} cities "
            f"({first_city} -> {last_city})",
        )
        last_err = None
        for attempt in range(retries + 1):
            try:
                payloads = fetch_chunk(chunk, year=year, timeout=timeout, models=models)
                break
            except Exception as exc:
                last_err = exc
                if attempt >= retries:
                    print_progress(progress, f"Main download chunk {chunk_no}/{total_chunks} failed after {attempt + 1} attempts: {exc}")
                    raise RuntimeError(f"Failed chunk {i // chunk_size + 1} after {retries + 1} attempts: {exc}") from exc
                wait = retry_wait_seconds(exc, attempt)
                print_progress(
                    progress,
                    f"Main download chunk {chunk_no}/{total_chunks} attempt {attempt + 1}/{retries + 1} failed: "
                    f"{exc}; waiting {format_elapsed(wait)} before retry",
                )
                time.sleep(wait)
        else:
            raise RuntimeError(str(last_err))

        if len(payloads) != len(chunk):
            raise RuntimeError(f"Chunk response count mismatch: expected {len(chunk)}, got {len(payloads)}")

        for target, payload in zip(chunk, payloads):
            data[target["city_name"]] = values_from_payload(target, payload, expected)

        # 每完成一个 chunk 立即写入临时文件，防止中途失败后全部丢失
        if output:
            write_wide_csv_atomic(data, output)
            print_progress(progress, f"Checkpoint written: {output}")

        time.sleep(pause)

        print_progress(
            progress,
            f"Finished main download chunk {chunk_no}/{total_chunks}; elapsed {format_elapsed(time.time() - started)}, "
            f"estimated remaining {estimate_remaining(started, chunk_no, total_chunks)}",
        )
    return data


def write_targets(targets: List[Dict[str, Any]], path: str) -> None:
    fields = [
        "country_area", "region", "city_name", "coastal_class",
        "sea_latitude", "sea_longitude", "backup_sea_latitude", "backup_sea_longitude",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(targets)


def write_wide_csv(data: Dict[str, List[Any]], output: str) -> None:
    columns = list(data.keys())
    n = len(data["timestamp"])
    with open(output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for idx in range(n):
            writer.writerow([data[col][idx] for col in columns])


def read_wide_csv(path: str) -> Dict[str, List[Any]]:
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []
        if "timestamp" not in columns:
            raise ValueError(f"Output CSV must contain a timestamp column: {path}")
        data: Dict[str, List[Any]] = {col: [] for col in columns}
        for row in reader:
            for col in columns:
                data[col].append(row.get(col, ""))
    return data


def prepare_incremental_data(
    output: str,
    targets: List[Dict[str, Any]],
    year: int,
    progress: bool,
) -> Tuple[Dict[str, List[Any]], List[Dict[str, Any]], int]:
    expected = expected_timestamps(year)
    data: Dict[str, List[Any]] = {"timestamp": expected}
    existing: Dict[str, List[Any]] = {}
    existing_timestamps: List[str] = []

    if os.path.exists(output):
        try:
            existing = read_wide_csv(output)
            existing_timestamps = [str(ts) for ts in existing.get("timestamp", [])]
            print_progress(
                progress,
                f"Loaded existing output for incremental run: {output}; rows={len(existing_timestamps)}",
            )
        except Exception as exc:
            print_progress(progress, f"Existing output could not be read and will be ignored: {output}; {exc}")

    timestamp_index = {ts: idx for idx, ts in enumerate(existing_timestamps)}
    download_targets: List[Dict[str, Any]] = []
    skipped_targets = 0

    for target in targets:
        city = target["city_name"]
        values = existing.get(city)
        aligned = [""] * len(expected)
        if values is not None:
            if timestamp_index:
                for out_idx, ts in enumerate(expected):
                    in_idx = timestamp_index.get(ts)
                    if in_idx is not None and in_idx < len(values):
                        aligned[out_idx] = values[in_idx]
            elif len(values) == len(expected):
                aligned = list(values)

        if count_missing_values(aligned) < len(aligned):
            skipped_targets += 1
        else:
            download_targets.append(target)
        data[city] = aligned

    print_progress(
        progress,
        f"Incremental selection: skipped {skipped_targets} cities with existing data; "
        f"will download {len(download_targets)} fully missing cities",
    )
    return data, download_targets, skipped_targets


def has_backup_coordinates(target: Dict[str, Any]) -> bool:
    lat = target.get("backup_sea_latitude")
    lon = target.get("backup_sea_longitude")
    return not is_missing_value(lat) and not is_missing_value(lon)


def target_with_backup_coordinates(target: Dict[str, Any]) -> Dict[str, Any]:
    candidate = dict(target)
    candidate["sea_latitude"] = float(str(target["backup_sea_latitude"]).strip())
    candidate["sea_longitude"] = float(str(target["backup_sea_longitude"]).strip())
    return candidate


def write_template(targets: List[Dict[str, Any]], year: int, output: str) -> None:
    timestamps = expected_timestamps(year)
    columns = ["timestamp"] + [t["city_name"] for t in targets]
    with open(output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for ts in timestamps:
            writer.writerow([ts] + [""] * len(targets))


def fetch_single_target_values(
    target: Dict[str, Any],
    year: int,
    timeout: int,
    retries: int,
    models: str | None,
    progress: bool = False,
) -> List[Any]:
    expected = expected_timestamps(year)
    last_err = None
    for attempt in range(retries + 1):
        try:
            payloads = fetch_chunk([target], year=year, timeout=timeout, models=models)
            if len(payloads) != 1:
                raise RuntimeError(f"Single-target response count mismatch: expected 1, got {len(payloads)}")
            return values_from_payload(target, payloads[0], expected)
        except Exception as exc:
            last_err = exc
            if attempt >= retries:
                raise
            wait = retry_wait_seconds(exc, attempt)
            print_progress(
                progress,
                f"Backup download for {target['city_name']} attempt {attempt + 1}/{retries + 1} failed: "
                f"{exc}; waiting {format_elapsed(wait)} before retry",
            )
            time.sleep(wait)
    raise RuntimeError(str(last_err))


def repair_full_missing_with_backup_coordinates(
    output: str,
    targets: List[Dict[str, Any]],
    year: int,
    timeout: int,
    pause: float,
    retries: int,
    models: str | None,
    verbose: bool,
    progress: bool = True,
) -> Tuple[int, int]:
    data = read_wide_csv(output)
    target_by_city = {target["city_name"]: target for target in targets}
    full_missing_by_city = {
        city: count_missing_values(values)
        for city, values in data.items()
        if city != "timestamp" and count_missing_values(values) == len(values)
    }
    print_progress(progress, f"Backup-coordinate repair scan: {len(full_missing_by_city)} fully missing city columns")
    if not full_missing_by_city:
        return 0, 0

    repaired = 0
    started = time.time()
    full_missing_items = sorted(full_missing_by_city.items())
    for idx, (city, original_missing) in enumerate(full_missing_items, start=1):
        target = target_by_city.get(city)
        if target is None or not has_backup_coordinates(target):
            print_progress(progress or verbose, f"Backup repair {idx}/{len(full_missing_items)}: no backup coordinate for {city}")
            continue
        candidate = target_with_backup_coordinates(target)
        print_progress(
            progress,
            f"Backup repair {idx}/{len(full_missing_items)}: downloading {city} "
            f"at ({candidate['sea_latitude']:.6f}, {candidate['sea_longitude']:.6f})",
        )
        try:
            values = fetch_single_target_values(
                target=candidate,
                year=year,
                timeout=timeout,
                retries=retries,
                models=models,
                progress=progress,
            )
        except Exception as exc:
            print_progress(progress, f"Backup repair {idx}/{len(full_missing_items)} failed for {city}: {exc}")
            time.sleep(pause)
            continue
        missing = count_missing_values(values)
        if missing < original_missing:
            data[city] = values
            repaired += 1
            print_progress(
                progress or verbose,
                f"Backup repair improved {city}: missing {original_missing}->{missing}",
            )
        elif verbose:
            print_progress(True, f"Backup coordinate did not improve {city}: missing {original_missing}->{missing}")
        time.sleep(pause)
        print_progress(
            progress,
            f"Finished backup repair {idx}/{len(full_missing_items)}; elapsed {format_elapsed(time.time() - started)}, "
            f"estimated remaining {estimate_remaining(started, idx, len(full_missing_items))}",
        )

    if repaired:
        print_progress(progress, f"Writing CSV after backup-coordinate repair: {output}")
        write_wide_csv_atomic(data, output)
    return repaired, len(full_missing_by_city)


def parse_float_or_none(value: Any) -> float | None:
    if is_missing_value(value):
        return None
    try:
        return float(value)
    except Exception:
        return None


def format_float(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def fill_internal_missing_values(output: str, verbose: bool, progress: bool = True) -> Tuple[int, int, int]:
    data = read_wide_csv(output)
    filled_cells = 0
    unresolved_columns = 0
    unresolved_cells = 0
    missing_columns = [
        city for city, values in data.items()
        if city != "timestamp" and count_missing_values(values) > 0
    ]
    print_progress(progress, f"Interpolation scan: {len(missing_columns)} city columns contain missing values")

    started = time.time()
    for col_idx, city in enumerate(missing_columns, start=1):
        values = data[city]

        missing_before = count_missing_values(values)
        if missing_before == 0:
            continue

        numeric = [parse_float_or_none(value) for value in values]
        if all(value is None for value in numeric):
            unresolved_columns += 1
            unresolved_cells += len(values)
            print_progress(progress or verbose, f"Interpolation {col_idx}/{len(missing_columns)} skipped {city}: all values are missing")
            continue

        updated = list(values)
        i = 0
        while i < len(numeric):
            if numeric[i] is not None:
                i += 1
                continue
            start = i
            while i < len(numeric) and numeric[i] is None:
                i += 1
            end = i - 1
            left = start - 1
            right = end + 1
            if left < 0 or right >= len(numeric) or numeric[left] is None or numeric[right] is None:
                unresolved = end - start + 1
                unresolved_cells += unresolved
                continue
            left_value = numeric[left]
            right_value = numeric[right]
            assert left_value is not None and right_value is not None
            span = right - left
            for idx in range(start, end + 1):
                fraction = (idx - left) / span
                interpolated = left_value + (right_value - left_value) * fraction
                numeric[idx] = interpolated
                updated[idx] = format_float(interpolated)
                filled_cells += 1

        remaining = count_missing_values(updated)
        if remaining:
            unresolved_columns += 1
        data[city] = updated
        if verbose:
            print_progress(True, f"Interpolated {city}: missing {missing_before}->{remaining}")
        elif progress and (col_idx == 1 or col_idx == len(missing_columns) or col_idx % 25 == 0):
            print_progress(
                True,
                f"Interpolation progress {col_idx}/{len(missing_columns)}; elapsed {format_elapsed(time.time() - started)}, "
                f"estimated remaining {estimate_remaining(started, col_idx, len(missing_columns))}",
            )

    if filled_cells:
        print_progress(progress, f"Writing CSV after interpolation fill: {output}")
        write_wide_csv_atomic(data, output)
    return filled_cells, unresolved_columns, unresolved_cells


def summarize_output_missing_values(output: str) -> Dict[str, Any]:
    data = read_wide_csv(output)
    row_count = len(data.get("timestamp", []))
    city_counts = {
        city: count_missing_values(values)
        for city, values in data.items()
        if city != "timestamp"
    }
    full_missing = [city for city, count in city_counts.items() if count == row_count and row_count > 0]
    partial_missing = {city: count for city, count in city_counts.items() if 0 < count < row_count}
    return {
        "rows": row_count,
        "city_columns": len(city_counts),
        "full_missing_columns": full_missing,
        "partial_missing_columns": partial_missing,
        "missing_cells": sum(city_counts.values()),
    }

ROOT_DIR = Path(__file__).resolve().parent.parent
CITY_MAP_FILE = ROOT_DIR / "data" / "target_city_map.csv"

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=CITY_MAP_FILE)
    ap.add_argument("--sheet", default="Country_city_map")
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--output", default="sea_surface_temperature_2025_openmeteo.csv")
    ap.add_argument("--targets-output", default="coastal_city_sea_points.csv")
    ap.add_argument("--template-output", default="sea_surface_temperature_2025_TEMPLATE.csv")
    ap.add_argument("--chunk-size", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--pause", type=float, default=10)
    ap.add_argument("--retries", type=int, default=10)
    ap.add_argument("--models", default=None, help="Optional Open-Meteo model name; leave unset for best_match.")
    ap.add_argument("--save-targets", action="store_true", help="Write the resolved coastal city sea-point list. Default: disabled.")
    ap.add_argument("--save-template", action="store_true", help="Write a blank 8760-row SST template. Default: disabled.")
    ap.add_argument("--repair-missing", action=argparse.BooleanOptionalAction, default=True, help="After writing the output CSV, retry fully missing city columns using backup coordinates from the input CSV. Default: enabled.")
    ap.add_argument("--fill-missing", action=argparse.BooleanOptionalAction, default=True, help="Fill internal missing SST gaps by linear interpolation after backup-coordinate repair. Default: enabled.")
    ap.add_argument("--repair-verbose", action="store_true", help="Print per-city repair and interpolation details. Default: disabled.")
    ap.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True, help="Print stage, retry, and progress messages while running. Default: enabled.")
    ap.add_argument("--dry-run", action="store_true", help="Resolve targets without downloading SST data. Use --save-targets or --save-template to write auxiliary files.")
    ap.add_argument("--resume", action="store_true", help="Deprecated compatibility flag; existing output CSV is always used for incremental runs.")
    args = ap.parse_args()
    args.input = resolve_local_path(args.input)
    args.output = resolve_local_path(args.output)
    args.targets_output = resolve_local_path(args.targets_output)
    args.template_output = resolve_local_path(args.template_output)

    print_progress(
        args.progress,
        f"Starting SST collection: input={args.input}, output={args.output}, year={args.year}, "
        f"chunk_size={args.chunk_size}, pause={args.pause}s, retries={args.retries}",
    )
    targets = load_targets(args.input, args.sheet)
    backup_count = sum(1 for target in targets if has_backup_coordinates(target))
    total_chunks = (len(targets) + args.chunk_size - 1) // args.chunk_size
    print_progress(
        args.progress,
        f"Loaded {len(targets)} non-Inland targets; backup coordinates available for {backup_count} targets; "
        f"a full fresh download would use {total_chunks} chunks",
    )
    if args.save_targets:
        print_progress(args.progress, f"Writing target list: {args.targets_output}")
        write_targets(targets, args.targets_output)
        print(f"Wrote target list: {args.targets_output}")
    if args.save_template:
        print_progress(args.progress, f"Writing blank template: {args.template_output}")
        write_template(targets, args.year, args.template_output)
        print(f"Wrote blank template: {args.template_output}")

    if args.dry_run:
        print(f"Targets: {len(targets)} non-Inland cities")
        return 0

    data, download_targets, skipped_targets = prepare_incremental_data(
        output=args.output,
        targets=targets,
        year=args.year,
        progress=args.progress,
    )
    if download_targets:
        download_chunks = (len(download_targets) + args.chunk_size - 1) // args.chunk_size
        print_progress(
            args.progress,
            f"Incremental main download: {len(download_targets)} fully missing cities in {download_chunks} chunks; "
            f"{skipped_targets} cities will be kept from local output",
        )
        data = collect_sst(
            targets=download_targets,
            year=args.year,
            chunk_size=args.chunk_size,
            timeout=args.timeout,
            pause=args.pause,
            retries=args.retries,
            models=args.models,
            progress=args.progress,
            output=args.output,
            existing_data=data,
        )
    else:
        print_progress(args.progress, "Incremental main download skipped: every target already has at least one local data value")

    print_progress(args.progress, f"Writing merged SST CSV before repair/fill: {args.output}")
    write_wide_csv_atomic(data, args.output)
    print_progress(args.progress, f"Merged SST CSV written: {args.output}")
    if args.repair_missing:
        repaired, missing_columns = repair_full_missing_with_backup_coordinates(
            output=args.output,
            targets=targets,
            year=args.year,
            timeout=args.timeout,
            pause=args.pause,
            retries=args.retries,
            models=args.models,
            verbose=args.repair_verbose,
            progress=args.progress,
        )
        if missing_columns:
            print(f"SST backup-coordinate repair: repaired {repaired}/{missing_columns} fully missing city columns")
        else:
            print("SST backup-coordinate repair: no fully missing city columns")
    if args.fill_missing:
        filled_cells, unresolved_columns, unresolved_cells = fill_internal_missing_values(
            output=args.output,
            verbose=args.repair_verbose,
            progress=args.progress,
        )
        print(
            f"SST interpolation fill: filled {filled_cells} cells; "
            f"remaining unresolved columns={unresolved_columns}, cells={unresolved_cells}"
        )
    summary = summarize_output_missing_values(args.output)
    print_progress(
        args.progress,
        f"Final output check: rows={summary['rows']}, city_columns={summary['city_columns']}, "
        f"fully_missing_columns={len(summary['full_missing_columns'])}, "
        f"partially_missing_columns={len(summary['partial_missing_columns'])}, "
        f"missing_cells={summary['missing_cells']}",
    )
    if summary["full_missing_columns"]:
        print_progress(args.progress, "Fully missing columns: " + ", ".join(summary["full_missing_columns"]))
    if summary["partial_missing_columns"] and args.repair_verbose:
        details = ", ".join(f"{city}:{count}" for city, count in summary["partial_missing_columns"].items())
        print_progress(True, "Partially missing columns: " + details)
    print(f"Wrote SST CSV: {args.output}")
    print(f"Rows: {len(data['timestamp'])}, city columns: {len(data) - 1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
