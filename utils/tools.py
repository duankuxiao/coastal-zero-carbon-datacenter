import math
import re
import pandas as pd
from pathlib import Path

def _capacity_to_mw(value: object, column_name: str) -> float:
    amount = _number(value, column_name)
    unit = _capacity_unit_from_column(column_name)
    if unit == "gw":
        return amount * 1000.0
    if unit == "mw":
        return amount
    raise ValueError(f"Could not identify MW/GW unit from capacity column {column_name!r}.")


def _capacity_unit_from_column(column_name: str) -> str:
    normalized = _normalize_column(column_name)
    tokens = set(normalized.split("_"))
    if "gw" in tokens:
        return "gw"
    if "mw" in tokens:
        return "mw"
    return ""


def _scenario_label_from_column(column_name: str) -> str:
    label = re.sub(r"(?i).*2030[_\s-]*", "", str(column_name)).strip("_ -")
    return label or str(column_name)


def _find_column(columns: list[str], candidates: list[str], label: str) -> str:
    normalized = {_normalize_column(column): column for column in columns}
    for candidate in candidates:
        if _normalize_column(candidate) in normalized:
            return normalized[_normalize_column(candidate)]
    raise ValueError(
        f"Could not identify {label} column. Available columns: {', '.join(map(str, columns))}"
    )


def _number(value: object, label: str) -> float:
    try:
        number = float(value)
    except Exception as exc:
        raise ValueError(f"{label} must be numeric; got {value!r}.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite; got {value!r}.")
    return number


def _row_numeric_value(row: pd.Series | None, column: str) -> float:
    if row is None or column not in row.index:
        return math.nan
    try:
        return float(row[column])
    except Exception:
        return math.nan


def _numeric_sum(group: pd.DataFrame, column: str) -> float:
    if column not in group:
        return 0.0
    return float(pd.to_numeric(group[column], errors="coerce").fillna(0.0).sum())


def _numeric_mean(group: pd.DataFrame, column: str) -> float:
    if column not in group:
        return math.nan
    values = pd.to_numeric(group[column], errors="coerce")
    return float(values.mean()) if values.notna().any() else math.nan


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _is_ready(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _normalize_column(value: object) -> str:
    token = str(value).replace("\ufeff", "").strip().lower()
    token = re.sub(r"[^a-z0-9]+", "_", token)
    return token.strip("_")


def _row_value(row: pd.Series | None, column: str, default: object = math.nan) -> object:
    if row is None or column not in row.index:
        return default
    return row[column]


def _hours_token(hours: int | None) -> str:
    return "all_hours" if hours is None else f"{hours}h"


def _pct(numerator: float, denominator: object) -> float:
    denominator_float = float(denominator or 0.0)
    if math.isclose(denominator_float, 0.0):
        return math.nan
    return numerator / denominator_float * 100.0


def _resolve_baseline_alignment(start_time: str | None, time_alignment: str | None) -> str:
    if start_time:
        return "start_time"
    if time_alignment in (None, "sst"):
        return "sst"
    raise ValueError(
        "run_baseline compares air-source and seawater cooling on the SST time window. "
        "Use --time-alignment sst, or provide --start-time for a custom shared window."
    )


def _output_suffix(rated_it_power_kw: float, hours: int | None) -> str:
    power_token = _format_power_token(rated_it_power_kw)
    hours_token = _hours_token(hours)
    return f"{power_token}_{hours_token}"


def _format_power_token(rated_it_power_kw: float) -> str:
    if float(rated_it_power_kw).is_integer():
        return f"{int(rated_it_power_kw)}kW"
    return f"{rated_it_power_kw:g}kW".replace(".", "p")


def _resolve_path(path: str | Path, ROOT_DIR: str | Path ) -> Path:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = ROOT_DIR / resolved
    return resolved


def _metric_savings(
    baseline: dict[str, object],
    other: dict[str, object],
    metric: str,
) -> float:
    return float(baseline.get(metric, 0.0) or 0.0) - float(other.get(metric, 0.0) or 0.0)


def _resolve_output_dir(path: str | Path, ROOT_DIR: str | Path) -> Path:
    output_path = _resolve_path(path, ROOT_DIR)
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path


def _filename_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", str(value).strip())
    return token.strip("_") or "unknown"