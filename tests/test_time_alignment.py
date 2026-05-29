import numpy as np
import pandas as pd
import pytest

import energy.calculate_datacenter_energy as calc


CITY = "TestCity"


def _write_city_series(path, timestamps, values):
    pd.DataFrame({"timestamp": timestamps, CITY: values}).to_csv(path, index=False)
    return path


def _write_workload(path, hours=8760):
    pd.DataFrame({"cpu_load": np.full(hours, 0.5)}).to_csv(path, index=False)
    return path


def _patch_epw(monkeypatch):
    monkeypatch.setattr(
        calc,
        "_read_epw_dry_bulb_temperature",
        lambda city: np.arange(8760, dtype=float),
    )


def test_seawater_uses_sst_year_carbon_window(tmp_path, monkeypatch):
    _patch_epw(monkeypatch)
    workload = _write_workload(tmp_path / "workload.csv")
    old_times = pd.date_range("2015-01-01 00:00", periods=8760, freq="h")
    sst_times = pd.date_range("2025-01-01 00:00", periods=8760, freq="h")
    carbon = _write_city_series(
        tmp_path / "carbon.csv",
        old_times.append(sst_times),
        np.concatenate([np.full(8760, 100.0), np.full(8760, 500.0)]),
    )
    sst = _write_city_series(tmp_path / "sst.csv", sst_times, np.full(8760, 10.0))

    aligned = calc._resolve_aligned_inputs(
        city=CITY,
        cooling_type="seawater",
        workload_file=workload,
        hours=8760,
        start_time=None,
        time_alignment="sst",
        max_carbon_gap_hours=6,
        carbon_intensity_file=carbon,
        sst_file=sst,
        progress=False,
    )

    assert aligned["metadata"]["simulation_start_time"] == "2025-01-01 00:00:00"
    assert aligned["metadata"]["carbon_intensity_start_time"] == "2025-01-01 00:00:00"
    assert np.all(aligned["carbon_intensity"] == 500.0)


def test_sst_fraction_scales_aligned_seawater_temperature(tmp_path, monkeypatch):
    _patch_epw(monkeypatch)
    workload = _write_workload(tmp_path / "workload.csv", hours=3)
    timestamps = pd.date_range("2025-01-01 00:00", periods=3, freq="h")
    carbon = _write_city_series(tmp_path / "carbon.csv", timestamps, np.full(3, 100.0))
    sst = _write_city_series(tmp_path / "sst.csv", timestamps, np.array([10.0, 15.0, 20.0]))

    aligned = calc._resolve_aligned_inputs(
        city=CITY,
        cooling_type="seawater",
        workload_file=workload,
        hours=3,
        start_time=None,
        time_alignment="sst",
        max_carbon_gap_hours=6,
        sst_fraction=1.1,
        carbon_intensity_file=carbon,
        sst_file=sst,
        progress=False,
    )

    assert aligned["source_temperature"].tolist() == pytest.approx([11.0, 16.5, 22.0])


def test_carbon_gap_within_limit_is_interpolated(tmp_path, monkeypatch):
    _patch_epw(monkeypatch)
    workload = _write_workload(tmp_path / "workload.csv", hours=24)
    timestamps = pd.date_range("2025-01-01 00:00", periods=24, freq="h")
    values = np.arange(24, dtype=float)
    values[10:13] = np.nan
    carbon = _write_city_series(tmp_path / "carbon.csv", timestamps, values)
    sst = _write_city_series(tmp_path / "sst.csv", timestamps, np.full(24, 10.0))

    aligned = calc._resolve_aligned_inputs(
        city=CITY,
        cooling_type="seawater",
        workload_file=workload,
        hours=24,
        start_time=None,
        time_alignment="sst",
        max_carbon_gap_hours=6,
        carbon_intensity_file=carbon,
        sst_file=sst,
        progress=False,
    )

    assert not np.isnan(aligned["carbon_intensity"]).any()
    assert aligned["carbon_intensity"][10] == pytest.approx(10.0)
    assert aligned["carbon_intensity"][12] == pytest.approx(12.0)


def test_carbon_gap_above_limit_raises(tmp_path, monkeypatch):
    _patch_epw(monkeypatch)
    workload = _write_workload(tmp_path / "workload.csv", hours=24)
    timestamps = pd.date_range("2025-01-01 00:00", periods=24, freq="h")
    values = np.arange(24, dtype=float)
    values[6:18] = np.nan
    carbon = _write_city_series(tmp_path / "carbon.csv", timestamps, values)
    sst = _write_city_series(tmp_path / "sst.csv", timestamps, np.full(24, 10.0))

    with pytest.raises(ValueError, match="allowed 6-hour gap"):
        calc._resolve_aligned_inputs(
            city=CITY,
            cooling_type="seawater",
            workload_file=workload,
            hours=24,
            start_time=None,
            time_alignment="sst",
            max_carbon_gap_hours=6,
            carbon_intensity_file=carbon,
            sst_file=sst,
            progress=False,
        )


def test_air_source_latest_uses_last_hours(tmp_path, monkeypatch):
    _patch_epw(monkeypatch)
    workload = _write_workload(tmp_path / "workload.csv")
    old_times = pd.date_range("2015-01-01 00:00", periods=8760, freq="h")
    latest_times = pd.date_range("2025-01-01 00:00", periods=8760, freq="h")
    carbon = _write_city_series(
        tmp_path / "carbon.csv",
        old_times.append(latest_times),
        np.concatenate([np.full(8760, 100.0), np.full(8760, 300.0)]),
    )

    aligned = calc._resolve_aligned_inputs(
        city=CITY,
        cooling_type="air_source",
        workload_file=workload,
        hours=8760,
        start_time=None,
        time_alignment="latest",
        max_carbon_gap_hours=6,
        carbon_intensity_file=carbon,
        progress=False,
    )

    assert aligned["metadata"]["simulation_start_time"] == "2025-01-01 00:00:00"
    assert np.all(aligned["carbon_intensity"] == 300.0)


def test_start_time_alignment_starts_at_requested_hour(tmp_path, monkeypatch):
    _patch_epw(monkeypatch)
    workload = _write_workload(tmp_path / "workload.csv", hours=100)
    timestamps = pd.date_range("2025-01-01 00:00", periods=100, freq="h")
    carbon = _write_city_series(tmp_path / "carbon.csv", timestamps, np.arange(100, dtype=float))

    aligned = calc._resolve_aligned_inputs(
        city=CITY,
        cooling_type="air_source",
        workload_file=workload,
        hours=3,
        start_time="2025-01-02 00:00",
        time_alignment=None,
        max_carbon_gap_hours=6,
        carbon_intensity_file=carbon,
        progress=False,
    )

    assert aligned["metadata"]["time_alignment"] == "start_time"
    assert aligned["metadata"]["simulation_start_time"] == "2025-01-02 00:00:00"
    assert aligned["carbon_intensity"].tolist() == [24.0, 25.0, 26.0]


def test_epw_maps_by_day_of_year_and_hour():
    epw = np.arange(8760, dtype=float)
    timestamps = pd.DatetimeIndex(
        [
            pd.Timestamp("2025-01-01 00:00"),
            pd.Timestamp("2025-01-02 05:00"),
            pd.Timestamp("2025-12-31 23:00"),
        ]
    )

    mapped = calc._map_epw_to_timestamps(epw, timestamps, CITY)

    assert mapped.tolist() == [0.0, 29.0, 8759.0]
    with pytest.raises(ValueError, match="leap-day"):
        calc._map_epw_to_timestamps(
            epw,
            pd.DatetimeIndex([pd.Timestamp("2024-02-29 00:00")]),
            CITY,
        )


def test_city_timeseries_parses_supported_timestamp_formats(tmp_path):
    path = _write_city_series(
        tmp_path / "mixed.csv",
        [
            "2025-01-01 00:00",
            "2025-01-01T01:00",
            "2025-01-01T02:00:00Z",
        ],
        [1.0, 2.0, 3.0],
    )

    series = calc._read_city_timeseries(path, CITY, "mixed test")

    assert series.index.tolist() == [
        pd.Timestamp("2025-01-01 00:00"),
        pd.Timestamp("2025-01-01 01:00"),
        pd.Timestamp("2025-01-01 02:00"),
    ]
    assert series.tolist() == [1.0, 2.0, 3.0]
