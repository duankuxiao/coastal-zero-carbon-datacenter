#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Download and validate EPW weather files matched to the cities in sheet2 of 代表国家及城市.xlsx.

Input:  manifest_to_download.csv
Output: epw_files/*.epw, validated_manifest.csv, epw_files.zip

Validation target:
- Standard EPW text file
- 8760 hourly data rows
- non-leap year, no Feb 29
- numeric fields:
    column 7, zero-based index 6: outdoor dry-bulb temperature, degC
    column 9, zero-based index 8: relative humidity, %
    column 10, zero-based index 9: atmospheric pressure, Pa
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ImportError as exc:
    raise SystemExit("Missing dependency: requests. Install with: pip install requests") from exc


def validate_epw(path: Path) -> dict:
    """Return validation metadata for an EPW file."""
    result = {
        "actual_epw_path": str(path),
        "hourly_rows": 0,
        "has_feb29": False,
        "drybulb_col7_numeric": True,
        "rh_col9_numeric": True,
        "pressure_col10_numeric": True,
        "drybulb_min_C": None,
        "drybulb_max_C": None,
        "rh_min_pct": None,
        "rh_max_pct": None,
        "pressure_min_Pa": None,
        "pressure_max_Pa": None,
        "validation_status": "failed",
        "validation_notes": "",
    }

    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        result["validation_notes"] = f"read_error: {exc}"
        return result

    data_rows = []
    # Standard EPW has 8 header lines, then data rows. This fallback also skips any non-data rows.
    for line in raw:
        if not line or line.startswith(("LOCATION", "DESIGN CONDITIONS", "TYPICAL/EXTREME", "GROUND TEMPERATURES", "HOLIDAYS/DAYLIGHT", "COMMENTS", "DATA PERIODS")):
            continue
        parts = line.split(",")
        if len(parts) >= 10:
            try:
                int(float(parts[0])); int(float(parts[1])); int(float(parts[2])); int(float(parts[3]))
                data_rows.append(parts)
            except Exception:
                continue

    result["hourly_rows"] = len(data_rows)
    drys, rhs, ps = [], [], []
    notes = []
    for parts in data_rows:
        try:
            month = int(float(parts[1])); day = int(float(parts[2]))
            if month == 2 and day == 29:
                result["has_feb29"] = True
        except Exception:
            pass
        try:
            drys.append(float(parts[6]))
        except Exception:
            result["drybulb_col7_numeric"] = False
        try:
            rhs.append(float(parts[8]))
        except Exception:
            result["rh_col9_numeric"] = False
        try:
            ps.append(float(parts[9]))
        except Exception:
            result["pressure_col10_numeric"] = False

    if drys:
        result["drybulb_min_C"] = min(drys); result["drybulb_max_C"] = max(drys)
    if rhs:
        result["rh_min_pct"] = min(rhs); result["rh_max_pct"] = max(rhs)
    if ps:
        result["pressure_min_Pa"] = min(ps); result["pressure_max_Pa"] = max(ps)

    ok = True
    if result["hourly_rows"] != 8760:
        ok = False; notes.append(f"hourly_rows={result['hourly_rows']} not 8760")
    if result["has_feb29"]:
        ok = False; notes.append("contains Feb 29")
    for k in ("drybulb_col7_numeric", "rh_col9_numeric", "pressure_col10_numeric"):
        if not result[k]:
            ok = False; notes.append(f"{k}=False")
    # Range checks are soft warnings, not hard failures.
    if drys and (min(drys) < -100 or max(drys) > 80):
        notes.append("drybulb outside broad plausibility range [-100, 80] C")
    if rhs and (min(rhs) < 0 or max(rhs) > 110):
        notes.append("RH outside broad plausibility range [0, 110] %")
    if ps and (min(ps) < 30000 or max(ps) > 120000):
        notes.append("pressure outside broad plausibility range [30000, 120000] Pa")

    result["validation_status"] = "ok" if ok else "failed"
    result["validation_notes"] = "; ".join(notes)
    return result


def download_file(url: str, dest: Path, timeout: int = 90, retries: int = 3) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    headers = {"User-Agent": "Mozilla/5.0 EPW research downloader"}
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout, headers=headers) as r:
                r.raise_for_status()
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
            tmp.replace(dest)
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(min(2 * attempt, 8))
    raise RuntimeError(f"download failed after {retries} retries: {last_exc}")


def extract_epw_from_zip(zip_path: Path, cache_epw_path: Path) -> Path:
    cache_epw_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        epw_names = [n for n in z.namelist() if n.lower().endswith(".epw") and not n.endswith("/")]
        if not epw_names:
            raise RuntimeError(f"no .epw file inside {zip_path}")
        # Prefer the shortest name if multiple EPWs appear.
        epw_name = sorted(epw_names, key=len)[0]
        with z.open(epw_name) as src, cache_epw_path.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    return cache_epw_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="manifest_to_download.csv", help="CSV manifest generated from sheet2 matching")
    ap.add_argument("--out-dir", default="epw_files", help="directory for final city EPW files")
    ap.add_argument("--cache-dir", default="_download_cache", help="cache directory for source ZIPs and extracted station EPWs")
    ap.add_argument("--validated-manifest", default="validated_manifest.csv", help="output CSV with download and validation status")
    ap.add_argument("--zip-output", default="epw_files.zip", help="ZIP archive created from final EPW files")
    ap.add_argument("--limit", type=int, default=0, help="optional max number of city rows to process; 0 means all")
    ap.add_argument("--sleep", type=float, default=0.2, help="seconds to sleep between new source downloads")
    ap.add_argument("--save-cache", action="store_true", help="Keep source ZIP and extracted station EPW cache files. Default: disabled.")
    ap.add_argument("--save-epw-dir", action="store_true", help="Keep the final city EPW directory in addition to the ZIP archive. Default: disabled.")
    ap.add_argument("--save-validated-manifest", action="store_true", help="Keep validated_manifest.csv as a separate file in addition to the copy inside the ZIP archive. Default: disabled.")
    args = ap.parse_args()

    base = Path.cwd()
    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        raise SystemExit(f"Manifest not found: {manifest_path}")

    with manifest_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        input_fields = reader.fieldnames or []
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    temp_ctx = tempfile.TemporaryDirectory(prefix="epw_download_")
    temp_root = Path(temp_ctx.name)
    try:
        out_dir = Path(args.out_dir) if args.save_epw_dir else temp_root / "epw_files"
        out_dir.mkdir(parents=True, exist_ok=True)
        cache_dir = Path(args.cache_dir) if args.save_cache else temp_root / "_download_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        zip_cache = cache_dir / "zips"; zip_cache.mkdir(parents=True, exist_ok=True)
        epw_cache = cache_dir / "station_epw"; epw_cache.mkdir(parents=True, exist_ok=True)

        by_url = defaultdict(list)
        for row in rows:
            by_url[row["zip_url"]].append(row)

        source_epw_by_url = {}
        print(f"City rows: {len(rows)}; unique source ZIPs: {len(by_url)}")

        for idx, (url, linked_rows) in enumerate(by_url.items(), start=1):
            # URL basename is stable and unique enough for cache.
            zip_name = Path(urlparse(url).path).name
            zip_path = zip_cache / zip_name
            station_epw_name = zip_name[:-4] + ".epw" if zip_name.lower().endswith(".zip") else zip_name + ".epw"
            station_epw_path = epw_cache / station_epw_name
            if not station_epw_path.exists():
                if not zip_path.exists():
                    print(f"[{idx}/{len(by_url)}] downloading {zip_name}")
                    download_file(url, zip_path)
                    time.sleep(args.sleep)
                print(f"[{idx}/{len(by_url)}] extracting EPW from {zip_name}")
                extract_epw_from_zip(zip_path, station_epw_path)
            source_epw_by_url[url] = station_epw_path

        output_fields = list(input_fields)
        extra_fields = [
            "download_status", "validation_status",
            "actual_epw_path", "hourly_rows", "has_feb29",
            "drybulb_col7_numeric", "rh_col9_numeric", "pressure_col10_numeric",
            "drybulb_min_C", "drybulb_max_C", "rh_min_pct", "rh_max_pct",
            "pressure_min_Pa", "pressure_max_Pa", "validation_notes",
        ]
        for field in extra_fields:
            if field not in output_fields:
                output_fields.append(field)

        ok_count = 0
        for row in rows:
            try:
                src_epw = source_epw_by_url[row["zip_url"]]
                dest = out_dir / row["epw_filename"]
                shutil.copyfile(src_epw, dest)
                row["download_status"] = "downloaded"
                v = validate_epw(dest)
                if not args.save_epw_dir:
                    v["actual_epw_path"] = row["epw_filename"]
                row.update(v)
                if row.get("validation_status") == "ok":
                    ok_count += 1
            except Exception as exc:
                row["download_status"] = "failed"
                row["validation_status"] = "failed"
                row["validation_notes"] = str(exc)

        validated_path = Path(args.validated_manifest) if args.save_validated_manifest else temp_root / Path(args.validated_manifest).name
        with validated_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=output_fields)
            w.writeheader(); w.writerows(rows)

        zip_output = Path(args.zip_output)
        zip_output.parent.mkdir(parents=True, exist_ok=True)
        if zip_output.exists():
            zip_output.unlink()
        with zipfile.ZipFile(zip_output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for epw in sorted(out_dir.glob("*.epw")):
                z.write(epw, epw.name)
            z.write(validated_path, validated_path.name)

        print(f"Validated OK: {ok_count}/{len(rows)}")
        if args.save_epw_dir:
            print(f"EPW directory: {out_dir.resolve()}")
        if args.save_validated_manifest:
            print(f"Validated manifest: {validated_path.resolve()}")
        if args.save_cache:
            print(f"Download cache: {cache_dir.resolve()}")
        print(f"ZIP archive: {zip_output.resolve()}")
        return 0 if ok_count == len(rows) else 2
    finally:
        temp_ctx.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
