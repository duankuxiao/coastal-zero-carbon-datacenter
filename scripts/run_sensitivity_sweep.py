"""Run sensitivity sweeps for run.py configuration files."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from itertools import product
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from run import run_configured_cases


NUMERIC_PARAMETERS = (
    "idle_power_fraction",
    "sst_fraction",
    "load_shift_fraction",
    "wind_loss_fraction",
)
OBJECTIVE_PARAMETER = "optimization_objective"
LEVELS = ("low", "default", "high")
LEVEL_TOKENS = {
    "low": "low",
    "default": "base",
    "high": "high",
}
PARAMETER_TOKENS = {
    "idle_power_fraction": "idle",
    "sst_fraction": "sst",
    "load_shift_fraction": "shift",
    "wind_loss_fraction": "windloss",
}


@dataclass(frozen=True)
class SweepJob:
    label: str
    values: dict[str, object]


def _resolve_path(path: str | Path) -> Path:
    resolved = Path(path)
    if resolved.is_absolute():
        return resolved
    return ROOT_DIR / resolved


def _load_json_config(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError(f"Config file must contain one JSON object: {path}")
    return config


def _sensitivity_parameters(config: dict[str, object]) -> dict[str, object]:
    sensitivity = config.get("sensitivity_parameters")
    if not isinstance(sensitivity, dict):
        raise ValueError("Config must contain a 'sensitivity_parameters' object.")
    return sensitivity


def _parameter_config(sensitivity: dict[str, object], parameter: str) -> dict[str, object]:
    parameter_config = sensitivity.get(parameter)
    if not isinstance(parameter_config, dict):
        raise ValueError(f"sensitivity_parameters.{parameter} must be an object.")
    return parameter_config


def _level_value(
    config: dict[str, object],
    sensitivity: dict[str, object],
    parameter: str,
    level: str,
) -> object:
    parameter_config = _parameter_config(sensitivity, parameter)
    if level in parameter_config:
        return parameter_config[level]
    if level == "default" and parameter in config:
        return config[parameter]
    raise ValueError(f"sensitivity_parameters.{parameter} must define '{level}'.")


def _numeric_defaults(config: dict[str, object], sensitivity: dict[str, object]) -> dict[str, object]:
    return {
        parameter: _level_value(config, sensitivity, parameter, "default")
        for parameter in NUMERIC_PARAMETERS
    }


def _objective_values(sensitivity: dict[str, object]) -> list[str]:
    objective_config = _parameter_config(sensitivity, OBJECTIVE_PARAMETER)
    default = str(objective_config.get("default", "co2"))
    raw_alternatives = objective_config.get("alternatives", [])
    if isinstance(raw_alternatives, str):
        alternatives = [raw_alternatives]
    elif isinstance(raw_alternatives, list):
        alternatives = [str(value) for value in raw_alternatives]
    else:
        raise ValueError("sensitivity_parameters.optimization_objective.alternatives must be a list.")

    values: list[str] = []
    for value in [default, *alternatives]:
        if value not in values:
            values.append(value)
    return values


def _safe_label(value: object) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value).strip().lower())
    return label.strip("-") or "value"


def _one_at_a_time_jobs(config: dict[str, object]) -> list[SweepJob]:
    sensitivity = _sensitivity_parameters(config)
    defaults = _numeric_defaults(config, sensitivity)
    objectives = _objective_values(sensitivity)
    default_objective = objectives[0]

    jobs = [
        SweepJob(
            label="baseline",
            values={**defaults, OBJECTIVE_PARAMETER: default_objective},
        )
    ]

    for parameter in NUMERIC_PARAMETERS:
        for level in ("low", "high"):
            values = dict(defaults)
            values[parameter] = _level_value(config, sensitivity, parameter, level)
            values[OBJECTIVE_PARAMETER] = default_objective
            jobs.append(SweepJob(label=f"{parameter}_{level}", values=values))

    for objective in objectives[1:]:
        jobs.append(
            SweepJob(
                label=f"{OBJECTIVE_PARAMETER}_{_safe_label(objective)}",
                values={**defaults, OBJECTIVE_PARAMETER: objective},
            )
        )

    return jobs


def _full_factorial_jobs(config: dict[str, object]) -> list[SweepJob]:
    sensitivity = _sensitivity_parameters(config)
    numeric_options = [
        [
            (parameter, level, _level_value(config, sensitivity, parameter, level))
            for level in LEVELS
        ]
        for parameter in NUMERIC_PARAMETERS
    ]
    objective_options = [
        (OBJECTIVE_PARAMETER, _safe_label(objective), objective)
        for objective in _objective_values(sensitivity)
    ]

    jobs: list[SweepJob] = []
    for index, combination in enumerate(product(*numeric_options, objective_options), start=1):
        values = {parameter: value for parameter, _level, value in combination}
        label_parts = [
            f"{PARAMETER_TOKENS.get(parameter, parameter)}-{LEVEL_TOKENS.get(level, level)}"
            for parameter, level, _value in combination
            if parameter != OBJECTIVE_PARAMETER
        ]
        objective = values[OBJECTIVE_PARAMETER]
        label = f"combo_{index:03d}_{'_'.join(label_parts)}_objective-{_safe_label(objective)}"
        jobs.append(SweepJob(label=label, values=values))
    return jobs


def _build_jobs(config: dict[str, object], mode: str) -> list[SweepJob]:
    if mode == "one-at-a-time":
        return _one_at_a_time_jobs(config)
    if mode == "full-factorial":
        return _full_factorial_jobs(config)
    raise ValueError(f"Unsupported sweep mode: {mode}")


def _print_jobs(jobs: list[SweepJob]) -> None:
    for index, job in enumerate(jobs, start=1):
        values = json.dumps(job.values, ensure_ascii=False, sort_keys=True)
        print(f"{index:03d} {job.label}: {values}")
    print(f"Total jobs: {len(jobs)}")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run multiple sensitivity configurations through run.py."
    )
    parser.add_argument(
        "--config-file",
        default=str(ROOT_DIR / "scripts" / "sensitivity_run_config.txt"),
        help="Path to a JSON config with sensitivity_parameters.",
    )
    parser.add_argument(
        "--mode",
        choices=["one-at-a-time", "full-factorial"],
        default="one-at-a-time",
        help="Sweep strategy. full-factorial can create many runs.",
    )
    parser.add_argument("--output-dir", default=None, help="Base output directory for sweep results.")
    parser.add_argument("--list-jobs", action="store_true", help="Print planned jobs without running them.")
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N planned jobs.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--countries", nargs="+", default=None)
    parser.add_argument("--max-countries", type=int, default=None)
    parser.add_argument("--hours", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--include-not-ready", action="store_true")
    parser.add_argument("--write-debug-scale-results", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    config_path = _resolve_path(args.config_file)
    config = _load_json_config(config_path)
    jobs = _build_jobs(config, args.mode)
    if args.limit is not None:
        if args.limit <= 0:
            raise ValueError("--limit must be a positive integer.")
        jobs = jobs[: args.limit]

    if args.list_jobs:
        _print_jobs(jobs)
        return 0

    raw_output_dir = args.output_dir if args.output_dir is not None else config.get("output_dir", "results/sensitivity")
    base_output_dir = _resolve_path(str(raw_output_dir))
    all_outputs: dict[str, dict[str, str]] = {}

    for index, job in enumerate(jobs, start=1):
        print(f"[{index}/{len(jobs)}] Running {job.label}")
        output_files = run_configured_cases(
            config_file=config_path,
            output_dir=base_output_dir / job.label,
            include_not_ready=True if args.include_not_ready else None,
            dry_run=args.dry_run,
            idle_power_fraction=float(job.values["idle_power_fraction"]),
            hours=args.hours,
            sst_fraction=float(job.values["sst_fraction"]),
            load_shift_fraction=float(job.values["load_shift_fraction"]),
            wind_loss_fraction=float(job.values["wind_loss_fraction"]),
            optimization_objective=str(job.values[OBJECTIVE_PARAMETER]),
            workers=args.workers,
            countries=args.countries,
            max_countries=args.max_countries,
            write_debug_scale_results=True if args.write_debug_scale_results else None,
        )
        all_outputs[job.label] = {key: str(path) for key, path in output_files.items()}

    print(json.dumps(all_outputs, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
