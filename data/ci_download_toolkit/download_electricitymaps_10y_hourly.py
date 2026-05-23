#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Download hourly grid carbon intensity from Electricity Maps /carbon-intensity/past-range.

Output schema:
    timestamp,city_name1,city_name2,...
    2016-05-15 00:00,320,200,...

Important API behavior handled by this version:
- Query by zone or by lat/lon.
- Hourly past-range calls are chunked to <=10 days.
- Coordinates may resolve to a zone that is not exposed by carbon-intensity/past-range
  for your API token or not supported by Electricity Maps history. Example: Chile
  Magallanes may resolve to CL-SEM while available carbon intensity coverage only exposes
  CL-SEN. This version detects such zones and, by default, leaves those cities blank instead
  of repeatedly failing every chunk.
- Optional explicit zone fallback mapping is supported, but it is not applied silently.

Recommended usage:
    export ELECTRICITYMAPS_TOKEN="your_token"
    python download_electricitymaps_10y_hourly_v2.py \
        --manifest city_electricitymaps_request_manifest.csv \
        --output-dir electricitymaps_10y_output_v2 \
        --output-wide city_grid_carbon_intensity_electricitymaps_10y.csv \
        --end now \
        --years-back 10 \
        --emission-factor-type direct

Optional proxy mapping usage:
    python download_electricitymaps_10y_hourly_v2.py ... \
        --zone-fallback-map zone_fallback_map.csv \
        --unavailable-zone-action fallback_map
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

API_BASE = "https://api.electricitymaps.com"
DEFAULT_API_VERSION = "v3"


class APIRequestError(RuntimeError):
    def __init__(self, status_code: Optional[int], body: str, url: str):
        self.status_code = status_code
        self.body = body
        self.url = url
        super().__init__(self._format())

    def _format(self) -> str:
        prefix = f"HTTP {self.status_code}" if self.status_code else "Request error"
        return f"{prefix}: {self.body[:1000]}\nURL: {self.url}"


def parse_datetime_utc(value: str) -> dt.datetime:
    """Parse 'now', date, or ISO datetime and return an hour-aligned UTC datetime."""
    v = str(value).strip()
    if v.lower() == "now":
        return dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return dt.datetime.fromisoformat(v).replace(tzinfo=dt.timezone.utc)
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    x = dt.datetime.fromisoformat(v)
    if x.tzinfo is None:
        x = x.replace(tzinfo=dt.timezone.utc)
    return x.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)


def iso_z(x: dt.datetime) -> str:
    return x.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def output_ts(x: dt.datetime) -> str:
    return x.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def clean_header(s: str) -> str:
    return str(s).replace("\ufeff", "").strip()


def safe_name(s: str, max_len: int = 120) -> str:
    s = str(s).strip()
    s = re.sub(r"[^A-Za-z0-9._=-]+", "_", s)
    return s[:max_len] or "unknown"


def read_manifest(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = [{clean_header(k): (v.strip() if isinstance(v, str) else v) for k, v in row.items()} for row in reader]
    if not rows:
        raise ValueError(f"Manifest is empty: {path}")

    aliases = {
        "city_name": ["city_name", "city", "City", "城市名称"],
        "output_column": ["output_column", "column_name", "city_name", "City", "城市名称"],
        "lat": ["city_latitude", "latitude", "lat", "Latitude", "纬度"],
        "lon": ["city_longitude", "longitude", "lon", "Longitude", "经度"],
        "zone": ["electricitymaps_zone", "zone", "zone_key", "zoneKey"],
        "zone_hint": ["zone_hint", "country_zone", "country_code"],
    }

    def pick(row: Dict[str, str], names: List[str]) -> str:
        for n in names:
            if n in row and row[n] not in {None, ""}:
                return str(row[n]).strip()
        return ""

    normalized: List[Dict[str, str]] = []
    for i, row in enumerate(rows, start=1):
        city_name = pick(row, aliases["city_name"])
        lat = pick(row, aliases["lat"])
        lon = pick(row, aliases["lon"])
        out_col = pick(row, aliases["output_column"]) or city_name
        zone = pick(row, aliases["zone"])
        zone_hint = pick(row, aliases["zone_hint"])
        if not city_name:
            city_name = f"city_{i}"
        if not lat or not lon:
            raise ValueError(f"Missing latitude/longitude at row {i}: {row}")
        normalized.append({
            "city_index": str(i),
            "city_name": city_name,
            "output_column": out_col,
            "lat": lat,
            "lon": lon,
            "zone": zone,
            "zone_hint": zone_hint,
        })
    return normalized


def read_zone_fallback_map(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Zone fallback map does not exist: {p}")
    mapping: Dict[str, str] = {}
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = [clean_header(h) for h in (reader.fieldnames or [])]
        if "source_zone" not in headers or "fallback_zone" not in headers:
            raise ValueError("Zone fallback map must contain columns: source_zone,fallback_zone")
        for row in reader:
            src = (row.get("source_zone") or "").strip()
            dst = (row.get("fallback_zone") or "").strip()
            if src and dst:
                mapping[src] = dst
    return mapping


def build_url(api_base: str, api_version: str, endpoint: str, params: Dict[str, Any]) -> str:
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None and v != ""})
    return f"{api_base.rstrip('/')}/{api_version.strip('/')}/{endpoint.lstrip('/')}?{q}"


def request_json(url: str, token: str, retries: int, timeout: int, sleep_base: float) -> Any:
    headers = {
        "auth-token": token,
        "Accept": "application/json",
        "User-Agent": "coastal-data-center-research/2.0",
    }
    last_error: Optional[APIRequestError] = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            err = APIRequestError(e.code, body, url)
            last_error = err
            if e.code in {408, 429, 500, 502, 503, 504} and attempt < retries:
                retry_after = e.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = min(300.0, float(retry_after))
                else:
                    wait = min(300.0, sleep_base * (2 ** attempt))
                print(f"Retry {attempt + 1}/{retries}: {err}; wait {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise err from e
        except Exception as e:
            err = APIRequestError(None, f"{type(e).__name__}: {e}", url)
            last_error = err
            if attempt < retries:
                wait = min(300.0, sleep_base * (2 ** attempt))
                print(f"Retry {attempt + 1}/{retries}: {err}; wait {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise err from e
    assert last_error is not None
    raise last_error


def extract_zone(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("zone", "zoneKey", "zoneId", "id"):
            v = payload.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for key in ("data", "history", "entries", "results", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                for p in value:
                    z = extract_zone(p)
                    if z:
                        return z
            elif isinstance(value, dict):
                z = extract_zone(value)
                if z:
                    return z
    return None


def extract_available_zones(payload: Any) -> Set[str]:
    zones: Set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            # Common /zones response shape can be a dict keyed by zone id.
            for k, v in x.items():
                if isinstance(k, str) and re.fullmatch(r"[A-Z]{2}(-[A-Z0-9]+)*", k):
                    zones.add(k)
                if k in {"zone", "zoneKey", "id"} and isinstance(v, str):
                    if re.fullmatch(r"[A-Z]{2}(-[A-Z0-9]+)*", v):
                        zones.add(v)
                walk(v)
        elif isinstance(x, list):
            for item in x:
                walk(item)

    walk(payload)
    return zones


def get_available_zones(
    token: str,
    api_base: str,
    api_version: str,
    retries: int,
    timeout: int,
    sleep_base: float,
) -> Set[str]:
    url = build_url(api_base, api_version, "zones", {})
    payload = request_json(url, token=token, retries=retries, timeout=timeout, sleep_base=sleep_base)
    zones = extract_available_zones(payload)
    if not zones:
        print("WARN /zones returned no recognizable zone keys; zone validation disabled.", file=sys.stderr)
    return zones


def extract_points(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for key in ("history", "data", "entries", "results", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [p for p in value if isinstance(p, dict)]
        if any(k in payload for k in ("datetime", "timestamp", "startTime")):
            return [payload]
    raise RuntimeError(f"Unrecognized API response shape: {str(payload)[:800]}")


def point_to_ts_value(point: Dict[str, Any]) -> Tuple[str, Optional[float]]:
    raw_ts = point.get("datetime") or point.get("timestamp") or point.get("startTime")
    if not raw_ts:
        raise RuntimeError(f"Observation without datetime: {point}")
    x = parse_datetime_utc(str(raw_ts))
    value = None
    for key in ("carbonIntensity", "carbonIntensityDirect", "carbonIntensityLifecycle", "value"):
        if key in point:
            value = point.get(key)
            break
    if value is None:
        return output_ts(x), None
    try:
        return output_ts(x), float(value)
    except Exception:
        return output_ts(x), None


def iter_ranges(start: dt.datetime, end: dt.datetime, chunk_days: int) -> Iterable[Tuple[dt.datetime, dt.datetime]]:
    cur = start
    step = dt.timedelta(days=chunk_days)
    while cur < end:
        nxt = min(cur + step, end)
        yield cur, nxt
        cur = nxt


def expected_hourly_timestamps(start: dt.datetime, end: dt.datetime) -> List[str]:
    out: List[str] = []
    cur = start
    while cur < end:
        out.append(output_ts(cur))
        cur += dt.timedelta(hours=1)
    return out


def parse_invalid_zone_from_error(msg: str) -> Optional[str]:
    patterns = [
        r"Zone ['\"]([^'\"]+)['\"] does not exist",
        r"zone ['\"]([^'\"]+)['\"] does not exist",
        r"Unknown zone ['\"]([^'\"]+)['\"]",
        r"Invalid zone ['\"]([^'\"]+)['\"]",
    ]
    for pat in patterns:
        m = re.search(pat, msg)
        if m:
            return m.group(1)
    return None


@dataclass(frozen=True)
class QueryTarget:
    key: str
    zone: str
    lat: str
    lon: str
    unavailable_reason: str = ""

    def params_base(self) -> Dict[str, str]:
        if self.zone:
            return {"zone": self.zone}
        return {"lat": self.lat, "lon": self.lon, "disableCallerLookup": "true"}


def cache_key(target: QueryTarget, a: dt.datetime, b: dt.datetime, emission_factor_type: str, temporal_granularity: str) -> str:
    raw = "|".join([target.key, iso_z(a), iso_z(b), emission_factor_type, temporal_granularity])
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{safe_name(target.key)}__{a.strftime('%Y%m%dT%H%MZ')}__{b.strftime('%Y%m%dT%H%MZ')}__{h}.json"


def resolve_zone(
    row: Dict[str, str],
    token: str,
    api_base: str,
    api_version: str,
    retries: int,
    timeout: int,
    sleep_base: float,
    pause: float,
) -> Tuple[Optional[str], str]:
    """Resolve zone using manifest, then /zone by lat/lon, then latest by lat/lon."""
    if row.get("zone"):
        return row["zone"].strip(), "manifest"

    # Prefer the official coordinate-to-zone endpoint because it is designed for zone lookup.
    for endpoint, label, params in [
        ("zone", "api_zone_by_latlon", {"lat": row["lat"], "lon": row["lon"], "disableCallerLookup": "true"}),
        ("carbon-intensity/latest", "api_latest_by_latlon", {"lat": row["lat"], "lon": row["lon"], "disableCallerLookup": "true"}),
    ]:
        try:
            url = build_url(api_base, api_version, endpoint, params)
            payload = request_json(url, token=token, retries=retries, timeout=timeout, sleep_base=sleep_base)
            time.sleep(pause)
            zone = extract_zone(payload)
            if zone:
                return zone, label
        except Exception as e:
            # Continue to next method; final method returns unresolved.
            last = str(e).replace("\n", " | ")[:500]
            if endpoint == "carbon-intensity/latest":
                return None, f"no_zone_resolved: {last}"
    return None, "no_zone_resolved"


def resolve_target_for_city(
    row: Dict[str, str],
    resolved_zone: Optional[str],
    source: str,
    available_zones: Set[str],
    fallback_map: Dict[str, str],
    unavailable_zone_action: str,
) -> Tuple[str, str, str, str, str]:
    """Return target_key, final_zone, resolution_status, message, unavailable_reason."""
    zone = (resolved_zone or "").strip()
    message = source
    unavailable_reason = ""

    if zone:
        if not available_zones or zone in available_zones:
            return f"zone:{zone}", zone, "zone_resolved", message, ""

        # Zone was resolved but not available to this token/API coverage.
        if unavailable_zone_action == "fallback_map" and zone in fallback_map:
            fallback = fallback_map[zone]
            if available_zones and fallback not in available_zones:
                unavailable_reason = f"resolved_zone_not_available:{zone}; fallback_zone_not_available:{fallback}"
                return f"unavailable:{row['city_index']}:{zone}", "", "unavailable_zone", message, unavailable_reason
            return f"zone:{fallback}", fallback, "zone_fallback_map", f"{message}; fallback {zone}->{fallback}", ""

        if unavailable_zone_action == "country_hint":
            hint = (row.get("zone_hint") or "").strip()
            if hint and (not available_zones or hint in available_zones):
                return f"zone:{hint}", hint, "zone_country_hint_fallback", f"{message}; fallback {zone}->{hint}", ""

        if unavailable_zone_action == "error":
            raise RuntimeError(f"Resolved zone {zone} is not available in /zones. City={row['city_name']}")

        unavailable_reason = f"resolved_zone_not_available:{zone}"
        return f"unavailable:{row['city_index']}:{zone}", "", "unavailable_zone", message, unavailable_reason

    # No zone resolved. Direct lat/lon may still work; use it unless the user requested hard validation.
    return f"latlon:{row['lat']},{row['lon']}", "", "zone_unresolved_direct_latlon", message, ""


def fetch_target_series(
    target: QueryTarget,
    start: dt.datetime,
    end: dt.datetime,
    token: str,
    api_base: str,
    api_version: str,
    chunk_days: int,
    emission_factor_type: str,
    temporal_granularity: str,
    disable_estimations: bool,
    retries: int,
    timeout: int,
    sleep_base: float,
    pause: float,
    cache_dir: Optional[Path],
    log_writer: Optional[csv.DictWriter],
) -> Dict[str, Optional[float]]:
    series: Dict[str, Optional[float]] = {}
    target_cache_dir: Optional[Path] = None
    if cache_dir is not None:
        target_cache_dir = cache_dir / safe_name(target.key)
        target_cache_dir.mkdir(parents=True, exist_ok=True)

    if target.unavailable_reason:
        if log_writer is not None:
            log_writer.writerow({
                "target_key": target.key,
                "zone": target.zone,
                "lat": target.lat,
                "lon": target.lon,
                "start": iso_z(start),
                "end": iso_z(end),
                "status": "skipped_unavailable_zone",
                "n_points": 0,
                "used_cache": 0,
                "invalid_zone": target.unavailable_reason,
                "message": target.unavailable_reason,
            })
        print(f"SKIP target={target.key}: {target.unavailable_reason}", file=sys.stderr)
        return series

    stop_target = False
    for a, b in iter_ranges(start, end, chunk_days):
        if stop_target:
            break
        cache_file: Optional[Path] = None
        if target_cache_dir is not None:
            ck = cache_key(target, a, b, emission_factor_type, temporal_granularity)
            cache_file = target_cache_dir / ck
        status = "ok"
        message = ""
        invalid_zone = ""
        n_points = 0
        used_cache = cache_file.exists() if cache_file is not None else False
        try:
            if used_cache and cache_file is not None:
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
            else:
                params = target.params_base()
                params.update({
                    "start": iso_z(a),
                    "end": iso_z(b),
                    "temporalGranularity": temporal_granularity,
                    "emissionFactorType": emission_factor_type,
                })
                if disable_estimations:
                    params["disableEstimations"] = "true"
                url = build_url(api_base, api_version, "carbon-intensity/past-range", params)
                payload = request_json(url, token=token, retries=retries, timeout=timeout, sleep_base=sleep_base)
                if cache_file is not None:
                    cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                time.sleep(pause)

            points = extract_points(payload)
            n_points = len(points)
            for p in points:
                ts, value = point_to_ts_value(p)
                series[ts] = value
        except Exception as e:
            status = "error"
            message = str(e).replace("\n", " | ")[:2000]
            invalid_zone = parse_invalid_zone_from_error(message) or ""
            if invalid_zone:
                status = "invalid_zone_skipped"
                stop_target = True
            print(f"ERROR target={target.key} {iso_z(a)} -> {iso_z(b)}: {message}", file=sys.stderr)

        if log_writer is not None:
            log_writer.writerow({
                "target_key": target.key,
                "zone": target.zone,
                "lat": target.lat,
                "lon": target.lon,
                "start": iso_z(a),
                "end": iso_z(b),
                "status": status,
                "n_points": n_points,
                "used_cache": int(used_cache),
                "invalid_zone": invalid_zone,
                "message": message,
            })

    return series


def write_long_series(path: Path, target: QueryTarget, series: Dict[str, Optional[float]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "target_key", "zone", "lat", "lon", "carbon_intensity_gco2e_per_kwh"])
        for ts in sorted(series):
            v = series[ts]
            w.writerow([ts, target.key, target.zone, target.lat, target.lon, "" if v is None else v])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Loop over Electricity Maps /carbon-intensity/past-range to download hourly carbon intensity for the past 10 years."
    )
    parser.add_argument("--manifest", required=True, help="City manifest CSV. Required columns: city_name/output_column, latitude, longitude; optional electricitymaps_zone.")
    parser.add_argument("--output-dir", default="ci_download_toolkit", help="Output directory.")
    parser.add_argument("--output-wide", default="city_grid_carbon_intensity_electricitymaps_10y.csv", help="Wide city CSV filename inside output-dir.")
    parser.add_argument("--token", default=None, help="Electricity Maps API token. Prefer env var ELECTRICITYMAPS_TOKEN.")
    parser.add_argument("--api-base", default=API_BASE)
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--start", default=None, help="UTC start, inclusive. If omitted, computed as --end minus --years-back years approximately.")
    parser.add_argument("--end", default="now", help="UTC end, exclusive. Use 'now' or ISO datetime. Default: now.")
    parser.add_argument("--years-back", type=float, default=10.0, help="When --start is omitted, use this many years before --end. Default: 10.")
    parser.add_argument("--chunk-days", type=int, default=10, help="Hourly past-range maximum is 10 days; keep <=10. Default: 10.")
    parser.add_argument("--temporal-granularity", default="hourly", choices=["5_minutes", "15_minutes", "hourly", "daily", "weekly", "monthly", "quarterly", "yearly"])
    parser.add_argument("--emission-factor-type", default="direct", choices=["direct", "lifecycle"], help="direct for operational emissions; lifecycle for life-cycle emissions.")
    parser.add_argument("--disable-estimations", action="store_true", help="Request non-estimated data where supported. This may greatly reduce coverage.")
    parser.add_argument("--resolve-zones", action=argparse.BooleanOptionalAction, default=True, help="Resolve city coordinates to zone before downloading. Default true.")
    parser.add_argument("--validate-zones", action=argparse.BooleanOptionalAction, default=True, help="Call /zones and skip/fallback zones not available to the token. Default true.")
    parser.add_argument("--zone-fallback-map", default=None, help="Optional CSV with columns source_zone,fallback_zone. Used only when --unavailable-zone-action fallback_map.")
    parser.add_argument("--unavailable-zone-action", default="missing", choices=["missing", "fallback_map", "country_hint", "error"], help="How to handle resolved zones not available in /zones. Default: missing leaves output blank.")
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--sleep-base", type=float, default=2.0, help="Base seconds for exponential retry backoff.")
    parser.add_argument("--pause", type=float, default=0.25, help="Polite pause after successful non-cached requests.")
    parser.add_argument("--save-cache", action="store_true", help="Save and reuse per-request JSON cache files. Default: disabled.")
    parser.add_argument("--save-logs", action="store_true", help="Write diagnostic logs, zone resolution, missingness, metadata, and available-zones files. Default: disabled.")
    parser.add_argument("--save-long-output", action="store_true", help="Write one long-format CSV per API target. Default: disabled.")
    args = parser.parse_args()

    if args.temporal_granularity == "hourly" and args.chunk_days > 10:
        raise SystemExit("For hourly past-range, --chunk-days must be <= 10 according to Electricity Maps documentation.")
    if args.chunk_days <= 0:
        raise SystemExit("--chunk-days must be positive.")

    token = args.token or os.environ.get("ELECTRICITYMAPS_TOKEN")
    if not token:
        raise SystemExit("Missing API token. Set ELECTRICITYMAPS_TOKEN or pass --token.")

    end = parse_datetime_utc(args.end)
    if args.start:
        start = parse_datetime_utc(args.start)
    else:
        start = end - dt.timedelta(days=int(round(args.years_back * 365.25)))
        start = start.replace(minute=0, second=0, microsecond=0)
    if not start < end:
        raise SystemExit(f"Invalid range: start {iso_z(start)} must be before end {iso_z(end)}")

    rows = read_manifest(Path(args.manifest))
    fallback_map = read_zone_fallback_map(args.zone_fallback_map)

    out_dir = Path(args.output_dir)
    cache_dir = out_dir / "api_cache_json" if args.save_cache else None
    long_dir = out_dir / "target_long_csv" if args.save_long_output else None
    out_dir.mkdir(parents=True, exist_ok=True)
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
    if long_dir is not None:
        long_dir.mkdir(parents=True, exist_ok=True)

    available_zones: Set[str] = set()
    if args.validate_zones:
        try:
            available_zones = get_available_zones(
                token=token,
                api_base=args.api_base,
                api_version=args.api_version,
                retries=args.retries,
                timeout=args.timeout,
                sleep_base=args.sleep_base,
            )
            if args.save_logs:
                (out_dir / "available_zones.json").write_text(json.dumps(sorted(available_zones), indent=2), encoding="utf-8")
            print(f"Available zones returned by /zones: {len(available_zones)}", file=sys.stderr)
        except Exception as e:
            print(f"WARN failed to load /zones; continue without pre-validation. {e}", file=sys.stderr)
            available_zones = set()

    city_rows: List[Dict[str, str]] = []
    zone_resolution_rows: List[Dict[str, str]] = []
    for row in rows:
        initial_zone: Optional[str] = None
        source = "direct_latlon_no_zone_resolution"
        if args.resolve_zones:
            try:
                initial_zone, source = resolve_zone(
                    row=row,
                    token=token,
                    api_base=args.api_base,
                    api_version=args.api_version,
                    retries=args.retries,
                    timeout=args.timeout,
                    sleep_base=args.sleep_base,
                    pause=args.pause,
                )
            except Exception as e:
                initial_zone = None
                source = f"zone_resolution_error:{str(e).replace(chr(10), ' | ')[:1000]}"
                print(f"WARN zone resolution failed for {row['city_name']}; fallback to lat/lon. {source}", file=sys.stderr)

        target_key, final_zone, resolution_status, message, unavailable_reason = resolve_target_for_city(
            row=row,
            resolved_zone=initial_zone,
            source=source,
            available_zones=available_zones,
            fallback_map=fallback_map,
            unavailable_zone_action=args.unavailable_zone_action,
        )
        out_row = dict(row)
        out_row.update({
            "initial_resolved_zone": initial_zone or "",
            "resolved_zone": final_zone,
            "target_key": target_key,
            "resolution_status": resolution_status,
            "unavailable_reason": unavailable_reason,
        })
        city_rows.append(out_row)
        zone_resolution_rows.append({
            "city_index": row["city_index"],
            "city_name": row["city_name"],
            "output_column": row["output_column"],
            "lat": row["lat"],
            "lon": row["lon"],
            "initial_resolved_zone": initial_zone or "",
            "final_zone": final_zone,
            "target_key": target_key,
            "resolution_status": resolution_status,
            "message": message,
            "unavailable_reason": unavailable_reason,
        })

    zone_resolution_path = out_dir / "city_zone_resolution.csv"
    if args.save_logs:
        with zone_resolution_path.open("w", encoding="utf-8", newline="") as f:
            fields = [
                "city_index", "city_name", "output_column", "lat", "lon",
                "initial_resolved_zone", "final_zone", "target_key",
                "resolution_status", "message", "unavailable_reason"
            ]
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(zone_resolution_rows)

    target_by_key: Dict[str, QueryTarget] = {}
    for row in city_rows:
        key = row["target_key"]
        if key not in target_by_key:
            target_by_key[key] = QueryTarget(
                key=key,
                zone=row.get("resolved_zone", ""),
                lat=row["lat"],
                lon=row["lon"],
                unavailable_reason=row.get("unavailable_reason", ""),
            )

    print(f"Cities: {len(city_rows)}; unique API targets: {len(target_by_key)}", file=sys.stderr)
    print(f"Time range: {iso_z(start)} to {iso_z(end)} [end excluded]", file=sys.stderr)

    target_series: Dict[str, Dict[str, Optional[float]]] = {}
    log_path = out_dir / "download_log.csv"
    log_file = None
    log_w: Optional[csv.DictWriter] = None
    try:
        if args.save_logs:
            log_file = log_path.open("w", encoding="utf-8", newline="")
            fields = ["target_key", "zone", "lat", "lon", "start", "end", "status", "n_points", "used_cache", "invalid_zone", "message"]
            log_w = csv.DictWriter(log_file, fieldnames=fields)
            log_w.writeheader()
        for key, target in sorted(target_by_key.items()):
            print(f"Fetching {key}", file=sys.stderr)
            series = fetch_target_series(
                target=target,
                start=start,
                end=end,
                token=token,
                api_base=args.api_base,
                api_version=args.api_version,
                chunk_days=args.chunk_days,
                emission_factor_type=args.emission_factor_type,
                temporal_granularity=args.temporal_granularity,
                disable_estimations=args.disable_estimations,
                retries=args.retries,
                timeout=args.timeout,
                sleep_base=args.sleep_base,
                pause=args.pause,
                cache_dir=cache_dir,
                log_writer=log_w,
            )
            target_series[key] = series
            if long_dir is not None:
                write_long_series(long_dir / f"{safe_name(key)}.csv", target, series)
    finally:
        if log_file is not None:
            log_file.close()

    if args.temporal_granularity == "hourly":
        timestamps = expected_hourly_timestamps(start, end)
    else:
        timestamps = sorted({ts for s in target_series.values() for ts in s.keys()})

    columns = [row["output_column"] for row in city_rows]
    wide_path = out_dir / args.output_wide
    with wide_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp"] + columns)
        for ts in timestamps:
            row_vals: List[Any] = [ts]
            for city in city_rows:
                v = target_series.get(city["target_key"], {}).get(ts)
                row_vals.append("" if v is None else v)
            w.writerow(row_vals)

    missing_path = out_dir / "city_missingness_summary.csv"
    if args.save_logs:
        expected = len(timestamps)
        with missing_path.open("w", encoding="utf-8", newline="") as f:
            fields = [
                "city_name", "output_column", "target_key", "initial_resolved_zone", "resolved_zone",
                "resolution_status", "unavailable_reason", "expected_points", "available_points",
                "missing_points", "coverage_ratio"
            ]
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for city in city_rows:
                s = target_series.get(city["target_key"], {})
                available = sum(1 for ts in timestamps if s.get(ts) is not None)
                w.writerow({
                    "city_name": city["city_name"],
                    "output_column": city["output_column"],
                    "target_key": city["target_key"],
                    "initial_resolved_zone": city.get("initial_resolved_zone", ""),
                    "resolved_zone": city.get("resolved_zone", ""),
                    "resolution_status": city.get("resolution_status", ""),
                    "unavailable_reason": city.get("unavailable_reason", ""),
                    "expected_points": expected,
                    "available_points": available,
                    "missing_points": expected - available,
                    "coverage_ratio": round(available / expected, 6) if expected else "",
                })

        meta = {
            "api_base": args.api_base,
            "api_version": args.api_version,
            "endpoint": "carbon-intensity/past-range",
            "start_utc_inclusive": iso_z(start),
            "end_utc_exclusive": iso_z(end),
            "temporal_granularity": args.temporal_granularity,
            "emission_factor_type": args.emission_factor_type,
            "chunk_days": args.chunk_days,
            "validate_zones": args.validate_zones,
            "unavailable_zone_action": args.unavailable_zone_action,
            "zone_fallback_map": args.zone_fallback_map or "",
            "cities": len(city_rows),
            "unique_api_targets": len(target_by_key),
            "output_wide": str(wide_path),
            "unit": "gCO2eq/kWh",
            "timezone": "UTC",
        }
        (out_dir / "run_metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    print("Done.", file=sys.stderr)
    print(f"Wide output: {wide_path}")
    if args.save_logs:
        print(f"Zone resolution: {zone_resolution_path}")
        print(f"Missingness summary: {missing_path}")
        print(f"Download log: {log_path}")
    if long_dir is not None:
        print(f"Long target CSV directory: {long_dir}")
    if cache_dir is not None:
        print(f"API cache directory: {cache_dir}")


if __name__ == "__main__":
    main()
