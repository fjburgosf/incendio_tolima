"""Compare raw and differenced precipitation candidates inside each GRIB reference cycle."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import eccodes
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "era5_land"
SOURCES = [
    RAW / "era5_land_core_hourly_2026-06_tolima_screening_bbox.grib",
    RAW / "era5_land_core_hourly_2026-07_tolima_screening_bbox.grib",
    RAW / "era5_land_tp_hourly_2026-08_tolima_screening_bbox.grib",
    RAW / "era5_land_core_2026-08-31T20-23Z_supplement_check.grib",
    RAW / "era5_land_core_hourly_2026-09-01_tolima_screening_bbox.grib",
]
OUT = ROOT / "results" / "exploratory" / "diagnose_era5_precipitation_value_semantics_v1.json"
TARGET_GRID_INDEX = 624


def parse_datetime(date: int, time: int) -> datetime:
    return datetime.strptime(f"{date}{time:04d}", "%Y%m%d%H%M")


def main() -> None:
    if OUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing diagnostic: {OUT}")
    cycles: dict[datetime, dict[int, tuple[datetime, np.ndarray]]] = defaultdict(dict)
    target_trace = []
    for source in SOURCES:
        with source.open("rb") as handle:
            while message := eccodes.codes_grib_new_from_file(handle):
                try:
                    if str(eccodes.codes_get(message, "shortName")) != "tp":
                        continue
                    reference = parse_datetime(int(eccodes.codes_get(message, "dataDate")), int(eccodes.codes_get(message, "dataTime")))
                    validity = parse_datetime(int(eccodes.codes_get(message, "validityDate")), int(eccodes.codes_get(message, "validityTime")))
                    start = int(eccodes.codes_get(message, "startStep"))
                    end = int(eccodes.codes_get(message, "endStep"))
                    values = np.asarray(eccodes.codes_get_array(message, "values"), dtype=np.float64)
                    if end - start != 1 or end in cycles[reference]:
                        raise RuntimeError("Unexpected step structure")
                    cycles[reference][end] = (validity, values)
                    if reference.date().isoformat() in {"2026-06-01", "2026-06-02"}:
                        target_trace.append({"reference_utc": reference.isoformat() + "Z", "validity_utc": validity.isoformat() + "Z", "step_start": start, "step_end": end, "target_value_m": float(values[TARGET_GRID_INDEX])})
                finally:
                    eccodes.codes_release(message)
    raw_values, difference_values = [], []
    negative_by_end = defaultdict(int)
    reference_summary = []
    for reference, records in sorted(cycles.items()):
        ends = sorted(records)
        raw_total = np.zeros_like(records[ends[0]][1])
        diff_total = np.zeros_like(raw_total)
        negative = 0
        for end in ends:
            value = records[end][1]
            raw_total += value
            if end == 1:
                increment = value
            elif end - 1 in records:
                increment = value - records[end - 1][1]
            else:
                # The first retained timestamp is 00 UTC and belongs to a
                # forecast reference that began before the download window.
                # It is not eligible for a within-cycle difference check.
                continue
            diff_total += increment
            count = int(np.count_nonzero(increment < -1e-10))
            negative += count
            negative_by_end[end] += count
            raw_values.append(value)
            difference_values.append(increment)
        reference_summary.append({
            "reference_utc": reference.isoformat() + "Z",
            "step_end_minimum": min(ends),
            "step_end_maximum": max(ends),
            "raw_step_sum_m_spatial_mean": float(raw_total.mean()),
            "differenced_sum_m_spatial_mean": float(diff_total.mean()),
            "negative_differenced_cells": negative,
        })
    raw = np.concatenate([value.reshape(-1) for value in raw_values])
    diff = np.concatenate([value.reshape(-1) for value in difference_values])
    output = {
        "target_grid_index": TARGET_GRID_INDEX,
        "target_trace_first_two_reference_cycles": target_trace,
        "raw_candidate": {"minimum_m": float(raw.min()), "median_m": float(np.median(raw)), "maximum_m": float(raw.max()), "negative_cells": int(np.count_nonzero(raw < -1e-10))},
        "differenced_candidate": {"minimum_m": float(diff.min()), "median_m": float(np.median(diff)), "maximum_m": float(diff.max()), "negative_cells": int(np.count_nonzero(diff < -1e-10))},
        "negative_differenced_cells_by_step_end": {str(key): value for key, value in sorted(negative_by_end.items())},
        "first_five_reference_summaries": reference_summary[:5],
        "interpretation_limit": "The two numerical candidates are shown to diagnose a processing mechanism. This output does not choose either candidate as valid precipitation or support precipitation-event analyses.",
    }
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
