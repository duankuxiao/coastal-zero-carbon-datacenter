from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Iterable

import pandas as pd

from utils.tools import (
    _hours_token,
    _numeric_mean,
    _numeric_sum,
    _pct,
    _row_numeric_value,
    _row_value,
)


FINAL_OUTPUT_COLUMNS = [
    "country",
    "city",
    "growth_scenario",
    "coastal_datacenter_growth_capacity_mw",
    "cooling_energy_kwh",
    "server_energy_kwh",
    "total_energy_kwh",
    "cooling_carbon_emissions_kgco2",
    "server_carbon_emissions_kgco2",
    "total_carbon_emissions_kgco2",
    "required_wind_capacity_mw",
    "wind_annual_generation_mwh",
    "wind_curtailment_mwh",
    "renewable_physical_coverage_fraction",
    "grid_purchase_mwh",
    "grid_purchase_co2_kg",
]

FINAL_NUMERIC_COLUMNS = [
    column
    for column in FINAL_OUTPUT_COLUMNS
    if column not in {"country", "city", "growth_scenario"}
]

COOLING_METRICS = [
    "server_energy_kwh",
    "server_carbon_emissions_kgco2",
    "cooling_energy_kwh",
    "cooling_carbon_emissions_kgco2",
    "total_energy_kwh",
    "total_carbon_emissions_kgco2",
    "required_wind_capacity_mw",
    "wind_annual_generation_mwh",
]

AVERAGE_METRICS = {
    "average_grid_carbon_intensity_g_per_kwh",
    "renewable_physical_coverage_fraction",
    "load_movement_budget_used_fraction",
}

OPTIMIZATION_METHODS = {"baseline", "load_shift"}


def output_filename(
    *,
    level: str,
    cooling_type: str,
    optimization_method: str,
    hours: int | None,
    objective: str | None = None,
) -> str:
    """Return the shared final-output filename."""
    level_token = _filename_token(level)
    cooling_token = _filename_token(cooling_type)
    method_token = _filename_token(optimization_method)
    parts = [level_token, cooling_token, method_token]
    if objective and method_token != "baseline":
        parts.append(_objective_token(objective))
    parts.append(_hours_token(hours))
    return "_".join(parts) + ".csv"


def write_csv(frame: pd.DataFrame, path: Path) -> None:
    """Write a CSV atomically enough for repeated long-running refreshes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    frame.to_csv(temporary_path, index=False, encoding="utf-8-sig")
    temporary_path.replace(path)


def write_cooling_output_tables(
    city_results: pd.DataFrame,
    output_path: Path,
    *,
    hours: int | None,
    country_metric_aggregation: str = "mean",
    default_growth_scenario: str = "baseline",
    cooling_types: Iterable[str] = ("air_source", "seawater"),
) -> dict[str, Path]:
    """Write city/country final tables for air-source and seawater cooling."""
    normalized = normalize_final_rows(
        city_results,
        default_growth_scenario=default_growth_scenario,
    )
    files: dict[str, Path] = {}
    for cooling_type in cooling_types:
        city_subset = _filter_string_column(normalized, "cooling_type", cooling_type)
        country_subset = aggregate_country_final_rows(
            city_subset,
            country_metric_aggregation=country_metric_aggregation,
        )
        for level, frame in (("city", city_subset), ("country", country_subset)):
            filename = output_filename(
                level=level,
                cooling_type=cooling_type,
                optimization_method="baseline",
                hours=hours,
            )
            path = output_path / filename
            write_csv(frame[FINAL_OUTPUT_COLUMNS], path)
            files[f"{level}_{cooling_type}_baseline_csv"] = path
    return files


def write_optimization_output_tables(
    city_results: pd.DataFrame,
    output_path: Path,
    *,
    hours: int | None,
    country_metric_aggregation: str = "mean",
    default_growth_scenario: str = "baseline",
) -> dict[str, Path]:
    """Write city/country final tables for baseline and load-shift cases."""
    normalized = normalize_final_rows(
        city_results,
        default_growth_scenario=default_growth_scenario,
    )
    if normalized.empty:
        return {}

    normalized["optimization_method"] = normalized.apply(_optimization_method, axis=1)
    normalized = normalized[normalized["optimization_method"].isin(OPTIMIZATION_METHODS)].copy()
    if normalized.empty:
        return {}

    files: dict[str, Path] = {}
    baseline = normalized[normalized["optimization_method"] == "baseline"].copy()
    if not baseline.empty:
        baseline = _dedupe_case_rows(baseline)
        for cooling_type, cooling_rows in baseline.groupby("cooling_type", dropna=False, sort=True):
            _write_case_tables(
                files=files,
                rows=cooling_rows,
                output_path=output_path,
                hours=hours,
                country_metric_aggregation=country_metric_aggregation,
                cooling_type=str(cooling_type),
                optimization_method="baseline",
                objective=None,
            )

    optimized = normalized[normalized["optimization_method"] == "load_shift"].copy()
    if not optimized.empty:
        for (cooling_type, objective), case_rows in optimized.groupby(
            ["cooling_type", "objective"],
            dropna=False,
            sort=True,
        ):
            _write_case_tables(
                files=files,
                rows=case_rows,
                output_path=output_path,
                hours=hours,
                country_metric_aggregation=country_metric_aggregation,
                cooling_type=str(cooling_type),
                optimization_method="load_shift",
                objective=str(objective),
            )
    return files


def normalize_final_rows(
    results: pd.DataFrame,
    *,
    default_growth_scenario: str,
) -> pd.DataFrame:
    """Normalize heterogeneous script result rows to the final paper schema."""
    columns = [
        *FINAL_OUTPUT_COLUMNS,
        "cooling_type",
        "optimization_scenario",
        "scenario",
        "objective",
    ]
    if results.empty:
        return pd.DataFrame(columns=columns)

    rows = [_final_row(row, default_growth_scenario) for row in results.to_dict(orient="records")]
    frame = pd.DataFrame(rows)
    for column in columns:
        if column not in frame:
            frame[column] = math.nan if column in FINAL_NUMERIC_COLUMNS else ""
    return frame[columns]


def aggregate_country_final_rows(
    city_rows: pd.DataFrame,
    *,
    country_metric_aggregation: str,
) -> pd.DataFrame:
    """Aggregate normalized city rows to country rows."""
    if city_rows.empty:
        return pd.DataFrame(columns=city_rows.columns)
    mode = str(country_metric_aggregation).strip().lower()
    if mode not in {"mean", "sum"}:
        raise ValueError("country_metric_aggregation must be 'mean' or 'sum'.")

    group_columns = [
        column
        for column in ["country", "growth_scenario", "cooling_type", "optimization_scenario", "scenario", "objective"]
        if column in city_rows.columns
    ]
    rows: list[dict[str, object]] = []
    for _, group in city_rows.groupby(group_columns, dropna=False, sort=True):
        first = group.iloc[0].to_dict()
        row = {column: first.get(column, "") for column in group_columns}
        row["city"] = ""
        for metric in FINAL_NUMERIC_COLUMNS:
            values = pd.to_numeric(group[metric], errors="coerce") if metric in group else pd.Series(dtype=float)
            if metric == "renewable_physical_coverage_fraction":
                row[metric] = _aggregate_fraction(group, mode)
            elif mode == "sum":
                row[metric] = float(values.sum()) if values.notna().any() else math.nan
            else:
                row[metric] = float(values.mean()) if values.notna().any() else math.nan
        rows.append(row)
    frame = pd.DataFrame(rows)
    for column in city_rows.columns:
        if column not in frame:
            frame[column] = ""
    return frame[city_rows.columns]


def append_scale_totals(
    city_scale_results: pd.DataFrame,
    metric_columns: list[str],
    *,
    extra_group_columns: list[str],
) -> pd.DataFrame:
    """Append all-scale city totals while preserving scale-level rows."""
    if city_scale_results.empty:
        return city_scale_results.copy()

    group_columns = ["country", "growth_scenario", "city", *extra_group_columns]
    total_rows: list[dict[str, object]] = []
    for _, group in city_scale_results.groupby(group_columns, dropna=False, sort=True):
        first = group.iloc[0].to_dict()
        total_row = {column: first.get(column) for column in group_columns}
        total_row.update(
            {
                "scale": "all_scales",
                "city_count_in_country": first.get("city_count_in_country"),
                "country_growth_mw": first.get("country_growth_mw"),
                "city_growth_mw": first.get("city_growth_mw"),
                "scale_share": 1.0,
                "scale_capacity_mw": _numeric_sum(group, "scale_capacity_mw"),
                "facility_count": int(_numeric_sum(group, "facility_count")),
                "facility_capacity_mw": math.nan,
                "below_scale_min": bool(group["below_scale_min"].fillna(False).astype(bool).any()),
                "status": _combined_status(group),
                "error_message": _combine_errors(group),
            }
        )
        for metric in metric_columns:
            total_row[metric] = _aggregate_city_metric(group, metric)
        total_rows.append(total_row)

    return pd.concat([city_scale_results, pd.DataFrame(total_rows)], ignore_index=True, sort=False)


def select_all_scale_results(results: pd.DataFrame) -> pd.DataFrame:
    """Return only rows representing the combined all-scale result."""
    if results.empty or "scale" not in results:
        return results.copy().reset_index(drop=True)
    return results[results["scale"] == "all_scales"].copy().reset_index(drop=True)


def build_country_average_results(
    city_results: pd.DataFrame,
    *,
    metric_columns: list[str],
    extra_group_columns: list[str],
) -> pd.DataFrame:
    """Average city results within each country, scenario, and comparison group."""
    if city_results.empty:
        return city_results.copy()
    group_columns = ["country", "growth_scenario", *extra_group_columns]
    rows: list[dict[str, object]] = []
    for _, group in city_results.groupby(group_columns, dropna=False, sort=True):
        first = group.iloc[0].to_dict()
        city_count = int(group["city"].nunique()) if "city" in group else len(group)
        row = {column: first.get(column) for column in group_columns}
        row.update(
            {
                "representative_city_count": city_count,
                "country_growth_mw": first.get("country_growth_mw"),
                "average_city_growth_mw": _numeric_mean(group, "city_growth_mw"),
                "scale": first.get("scale"),
                "scale_share": first.get("scale_share"),
                "average_scale_capacity_mw": _numeric_mean(group, "scale_capacity_mw"),
                "average_facility_count": _numeric_mean(group, "facility_count"),
                "average_facility_capacity_mw": _numeric_mean(group, "facility_capacity_mw"),
                "below_scale_min_city_count": int(group["below_scale_min"].fillna(False).astype(bool).sum())
                if "below_scale_min" in group
                else 0,
                "status": _combined_status(group),
                "error_message": _combine_errors(group),
            }
        )
        for metric in metric_columns:
            row[metric] = _aggregate_country_metric(group, metric)
        rows.append(row)
    return pd.DataFrame(rows)


def build_cooling_comparison_results(results: pd.DataFrame) -> pd.DataFrame:
    """Compare seawater cooling against the air-source baseline."""
    if results.empty:
        return pd.DataFrame()
    group_columns = [
        column
        for column in ["country", "growth_scenario", "city", "scale"]
        if column in results.columns
    ]
    return _build_pairwise_comparison_results(
        results=results,
        group_columns=group_columns,
        compare_column="cooling_type",
        baseline_value="air_source",
        candidate_values=("seawater",),
        metric_columns=COOLING_METRICS,
        baseline_prefix="air_source",
        candidate_prefix="seawater",
        savings_suffix="vs_air_source",
    )


def build_optimization_comparison_results(results: pd.DataFrame) -> pd.DataFrame:
    """Compare optimization scenarios against the baseline scenario."""
    if results.empty:
        return pd.DataFrame()
    group_columns = [
        column
        for column in ["country", "growth_scenario", "city", "objective", "cooling_type", "scale"]
        if column in results.columns
    ]
    candidate_values = [
        str(value)
        for value in results["optimization_scenario"].dropna().unique()
        if str(value) not in {"baseline", "baseline_air_source"}
    ]
    return _build_pairwise_comparison_results(
        results=results,
        group_columns=group_columns,
        compare_column="optimization_scenario",
        baseline_value="baseline",
        candidate_values=tuple(sorted(candidate_values)),
        metric_columns=[
            column
            for column in results.columns
            if column
            not in {
                "country",
                "growth_scenario",
                "city",
                "objective",
                "cooling_type",
                "scale",
                "optimization_scenario",
                "optimization_scenario_label",
                "status",
                "error_message",
            }
            and pd.api.types.is_numeric_dtype(results[column])
        ],
        baseline_prefix="baseline",
        candidate_prefix_column="optimization_scenario",
        savings_suffix="vs_baseline",
        label_column="optimization_scenario_label",
    )


def _write_case_tables(
    *,
    files: dict[str, Path],
    rows: pd.DataFrame,
    output_path: Path,
    hours: int | None,
    country_metric_aggregation: str,
    cooling_type: str,
    optimization_method: str,
    objective: str | None,
) -> None:
    country_rows = aggregate_country_final_rows(
        rows,
        country_metric_aggregation=country_metric_aggregation,
    )
    for level, frame in (("city", rows), ("country", country_rows)):
        filename = output_filename(
            level=level,
            cooling_type=cooling_type,
            optimization_method=optimization_method,
            objective=objective,
            hours=hours,
        )
        path = output_path / filename
        write_csv(frame[FINAL_OUTPUT_COLUMNS], path)
        key_parts = [level, cooling_type, optimization_method]
        if objective and optimization_method != "baseline":
            key_parts.append(_objective_token(objective))
        files["_".join(key_parts) + "_csv"] = path


def _final_row(row: dict[str, object], default_growth_scenario: str) -> dict[str, object]:
    total_energy_kwh = _coalesce_number(row, ["total_energy_kwh", "datacenter_total_energy_kwh"])
    if _is_missing(total_energy_kwh):
        total_energy_mwh = _coalesce_number(row, ["datacenter_total_energy_mwh", "annual_demand_mwh"])
        total_energy_kwh = total_energy_mwh * 1000.0 if not _is_missing(total_energy_mwh) else math.nan

    wind_annual_generation_mwh = _coalesce_number(row, ["wind_annual_generation_mwh", "annual_generation_mwh", "annual_wind_mwh"])
    grid_purchase_mwh = _coalesce_number(row, ["grid_purchase_mwh"])
    grid_purchase_co2_kg = _coalesce_number(row, ["grid_purchase_co2_kg"])
    renewable_fraction = _coalesce_number(row, ["renewable_physical_coverage_fraction"])
    if _is_missing(renewable_fraction):
        demand_mwh = total_energy_kwh / 1000.0 if not _is_missing(total_energy_kwh) else math.nan
        if not _is_missing(demand_mwh) and demand_mwh > 0 and not _is_missing(grid_purchase_mwh):
            renewable_fraction = 1.0 - grid_purchase_mwh / demand_mwh
        elif not _is_missing(demand_mwh) and demand_mwh > 0 and not _is_missing(wind_annual_generation_mwh):
            renewable_fraction = min(wind_annual_generation_mwh / demand_mwh, 1.0)

    return {
        "country": row.get("country", row.get("country_area", "")),
        "city": row.get("city", ""),
        "growth_scenario": row.get("growth_scenario", default_growth_scenario) or default_growth_scenario,
        "coastal_datacenter_growth_capacity_mw": _capacity_mw(row),
        "cooling_energy_kwh": _coalesce_number(row, ["cooling_energy_kwh"]),
        "server_energy_kwh": _coalesce_number(row, ["server_energy_kwh"]),
        "total_energy_kwh": total_energy_kwh,
        "cooling_carbon_emissions_kgco2": _coalesce_number(row, ["cooling_carbon_emissions_kgco2"]),
        "server_carbon_emissions_kgco2": _coalesce_number(row, ["server_carbon_emissions_kgco2"]),
        "total_carbon_emissions_kgco2": _coalesce_number(row, ["total_carbon_emissions_kgco2"]),
        "required_wind_capacity_mw": _coalesce_number(row, ["required_wind_capacity_mw"]),
        "wind_annual_generation_mwh": wind_annual_generation_mwh,
        "wind_curtailment_mwh": _coalesce_number(row, ["wind_curtailment_mwh"], default=0.0),
        "renewable_physical_coverage_fraction": renewable_fraction,
        "grid_purchase_mwh": grid_purchase_mwh if not _is_missing(grid_purchase_mwh) else 0.0,
        "grid_purchase_co2_kg": grid_purchase_co2_kg if not _is_missing(grid_purchase_co2_kg) else 0.0,
        "cooling_type": row.get("cooling_type", ""),
        "optimization_scenario": row.get("optimization_scenario", row.get("scenario", "")),
        "scenario": row.get("scenario", ""),
        "objective": row.get("objective", ""),
    }


def _capacity_mw(row: dict[str, object]) -> float:
    value = _coalesce_number(
        row,
        [
            "coastal_datacenter_growth_capacity_mw",
            "city_growth_mw",
            "average_city_growth_mw",
            "scale_capacity_mw",
            "country_growth_mw",
        ],
    )
    if not _is_missing(value):
        return value
    rated_kw = _coalesce_number(row, ["rated_it_power_kw", "rated_it_power_kw_per_facility"])
    if not _is_missing(rated_kw):
        return rated_kw / 1000.0
    return math.nan


def _optimization_method(row: pd.Series) -> str:
    scenario = str(row.get("optimization_scenario") or row.get("scenario") or "").strip()
    if scenario == "baseline_air_source":
        return "baseline"
    normalized = scenario.lower().replace("-", "_")
    if normalized == "baseline":
        return "baseline"
    if normalized == "load_shift":
        return "load_shift"
    return normalized


def _dedupe_case_rows(rows: pd.DataFrame) -> pd.DataFrame:
    key_columns = [
        column
        for column in ["country", "city", "growth_scenario", "cooling_type", "optimization_method"]
        if column in rows.columns
    ]
    if not key_columns:
        return rows
    return rows.drop_duplicates(key_columns, keep="first").reset_index(drop=True)


def _aggregate_fraction(group: pd.DataFrame, mode: str) -> float:
    values = pd.to_numeric(group.get("renewable_physical_coverage_fraction"), errors="coerce")
    if mode == "mean":
        return float(values.mean()) if values.notna().any() else math.nan
    demand = pd.to_numeric(group.get("total_energy_kwh"), errors="coerce").sum() / 1000.0
    grid = pd.to_numeric(group.get("grid_purchase_mwh"), errors="coerce").sum()
    if demand:
        return float(1.0 - grid / demand)
    return float(values.mean()) if values.notna().any() else math.nan


def _filter_string_column(frame: pd.DataFrame, column: str, value: str) -> pd.DataFrame:
    if frame.empty or column not in frame:
        return pd.DataFrame(columns=frame.columns)
    return frame[frame[column].astype(str) == str(value)].copy().reset_index(drop=True)


def _coalesce_number(
    row: dict[str, object],
    keys: list[str],
    *,
    default: float = math.nan,
) -> float:
    for key in keys:
        if key not in row:
            continue
        value = row.get(key)
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return default


def _is_missing(value: object) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return value is None


def _filename_token(value: object) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", str(value).strip().lower())
    return token.strip("_") or "unknown"


def _objective_token(objective: str) -> str:
    normalized = str(objective).strip().lower()
    if "co2" in normalized:
        return "co2"
    if "mwh" in normalized or "energy" in normalized:
        return "mwh"
    return _filename_token(normalized)


def _aggregate_city_metric(group: pd.DataFrame, metric: str) -> float:
    if metric not in group:
        return math.nan
    values = pd.to_numeric(group[metric], errors="coerce")
    if metric in AVERAGE_METRICS:
        return float(values.mean()) if values.notna().any() else math.nan
    return float(values.sum()) if values.notna().any() else math.nan


def _aggregate_country_metric(group: pd.DataFrame, metric: str) -> float:
    if metric not in group:
        return math.nan
    values = pd.to_numeric(group[metric], errors="coerce")
    return float(values.mean()) if values.notna().any() else math.nan


def _combined_status(group: pd.DataFrame) -> str:
    if "status" not in group:
        return ""
    statuses = set(group["status"].dropna().astype(str))
    if statuses == {"ok"}:
        return "ok"
    if "failed" in statuses:
        return "failed"
    return "|".join(sorted(statuses))


def _combine_errors(group: pd.DataFrame) -> str:
    if "error_message" not in group:
        return ""
    errors = sorted({str(error) for error in group["error_message"].dropna() if str(error).strip()})
    return "; ".join(errors)


def _build_pairwise_comparison_results(
    *,
    results: pd.DataFrame,
    group_columns: list[str],
    compare_column: str,
    baseline_value: str,
    candidate_values: tuple[str, ...],
    metric_columns: list[str],
    baseline_prefix: str,
    savings_suffix: str,
    candidate_prefix: str | None = None,
    candidate_prefix_column: str | None = None,
    label_column: str | None = None,
) -> pd.DataFrame:
    if compare_column not in results.columns:
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    for _, group in results.groupby(group_columns, dropna=False, sort=True):
        baseline = _first_matching_row(group, compare_column, baseline_value)
        for candidate_value in candidate_values:
            candidate = _first_matching_row(group, compare_column, candidate_value)
            if baseline is None and candidate is None:
                continue
            source = candidate if candidate is not None else baseline
            assert source is not None
            row = _comparison_metadata(source, group_columns)
            metric_candidate_prefix = (
                "comparison"
                if candidate_prefix_column is not None
                else str(candidate_prefix or candidate_value)
            )
            row.update(
                {
                    "comparison": f"{candidate_value}_vs_{baseline_value}",
                    f"baseline_{compare_column}": baseline_value,
                    f"comparison_{compare_column}": candidate_value,
                    "baseline_status": _row_value(baseline, "status", "missing"),
                    "comparison_status": _row_value(candidate, "status", "missing"),
                    "status": _comparison_status(baseline, candidate),
                    "error_message": _comparison_error_message(baseline, candidate),
                }
            )
            if label_column:
                row[f"comparison_{label_column}"] = _row_value(candidate, label_column, candidate_value)
            for metric in metric_columns:
                baseline_metric = _row_numeric_value(baseline, metric)
                candidate_metric = _row_numeric_value(candidate, metric)
                savings = baseline_metric - candidate_metric
                row[f"{baseline_prefix}_{metric}"] = baseline_metric
                row[f"{metric_candidate_prefix}_{metric}"] = candidate_metric
                row[f"{metric}_savings_{savings_suffix}"] = savings
                row[f"{metric}_savings_pct_{savings_suffix}"] = _pct(savings, baseline_metric)
            rows.append(row)
    return pd.DataFrame(rows)


def _first_matching_row(group: pd.DataFrame, column: str, value: str) -> pd.Series | None:
    matches = group[group[column].astype(str) == value]
    if matches.empty:
        return None
    return matches.iloc[0]


def _comparison_metadata(source: pd.Series, group_columns: list[str]) -> dict[str, object]:
    return {column: source[column] for column in group_columns if column in source.index}


def _comparison_status(baseline: pd.Series | None, candidate: pd.Series | None) -> str:
    if baseline is None or candidate is None:
        return "failed"
    statuses = {str(_row_value(baseline, "status", "")), str(_row_value(candidate, "status", ""))}
    return "ok" if statuses == {"ok"} else "failed"


def _comparison_error_message(baseline: pd.Series | None, candidate: pd.Series | None) -> str:
    errors: list[str] = []
    if baseline is None:
        errors.append("Missing baseline row")
    if candidate is None:
        errors.append("Missing comparison row")
    for label, row in (("baseline", baseline), ("comparison", candidate)):
        message = str(_row_value(row, "error_message", "") or "").strip()
        if message:
            errors.append(f"{label}: {message}")
    return "; ".join(errors)
