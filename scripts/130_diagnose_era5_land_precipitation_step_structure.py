"""Diagnose ERA5 Land total-precipitation step semantics before reconstruction."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
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
OUT = ROOT / "results" / "exploratory" / "diagnose_era5_land_precipitation_step_structure_v1.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def parse_datetime(date: int, time: int) -> datetime:
    return datetime.strptime(f"{date}{time:04d}", "%Y%m%d%H%M")


def get_optional(message, key: str):
    try:
        return eccodes.codes_get(message, key)
    except eccodes.KeyValueNotFoundError:
        return None


def main() -> None:
    if OUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing diagnostic: {OUT}")
    records = []
    grid: tuple[np.ndarray, np.ndarray] | None = None
    for source in SOURCES:
        with source.open("rb") as handle:
            while message := eccodes.codes_grib_new_from_file(handle):
                try:
                    if str(eccodes.codes_get(message, "shortName")) != "tp":
                        continue
                    values = np.asarray(eccodes.codes_get_array(message, "values"), dtype=np.float64)
                    latitudes = np.asarray(eccodes.codes_get_array(message, "latitudes"), dtype=np.float64)
                    longitudes = np.asarray(eccodes.codes_get_array(message, "longitudes"), dtype=np.float64)
                    if grid is None:
                        grid = (latitudes, longitudes)
                    elif not (np.array_equal(latitudes, grid[0]) and np.array_equal(longitudes, grid[1])):
                        raise RuntimeError("Grid mismatch")
                    records.append(
                        {
                            "source": source.name,
                            "reference_utc": parse_datetime(int(eccodes.codes_get(message, "dataDate")), int(eccodes.codes_get(message, "dataTime"))),
                            "validity_utc": parse_datetime(int(eccodes.codes_get(message, "validityDate")), int(eccodes.codes_get(message, "validityTime"))),
                            "step_range": str(get_optional(message, "stepRange")),
                            "start_step": get_optional(message, "startStep"),
                            "end_step": get_optional(message, "endStep"),
                            "forecast_time": get_optional(message, "forecastTime"),
                            "step_type": str(get_optional(message, "stepType")),
                            "units": str(get_optional(message, "units")),
                            "mean_m": float(values.mean()),
                            "minimum_m": float(values.min()),
                            "maximum_m": float(values.max()),
                        }
                    )
                finally:
                    eccodes.codes_release(message)
    by_validity: dict[datetime, list[dict]] = defaultdict(list)
    for record in records:
        by_validity[record["validity_utc"]].append(record)
    duplicate_validities = {when.isoformat(): len(group) for when, group in by_validity.items() if len(group) != 1}
    sorted_records = sorted(records, key=lambda record: record["validity_utc"])
    differences = [
        (later["validity_utc"] - earlier["validity_utc"]).total_seconds() / 3600
        for earlier, later in zip(sorted_records, sorted_records[1:])
    ]
    output = {
        "source_files": [{"file": str(path.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(path)} for path in SOURCES],
        "record_count": len(records),
        "unique_validity_count": len(by_validity),
        "duplicate_or_nonunique_validities": duplicate_validities,
        "validity_hour_gap_counts": dict(Counter(str(value) for value in differences)),
        "step_range_counts": dict(Counter(record["step_range"] for record in records)),
        "start_step_counts": dict(Counter(str(record["start_step"]) for record in records)),
        "end_step_counts": dict(Counter(str(record["end_step"]) for record in records)),
        "forecast_time_counts": dict(Counter(str(record["forecast_time"]) for record in records)),
        "step_type_counts": dict(Counter(record["step_type"] for record in records)),
        "units": sorted({record["units"] for record in records}),
        "first_twelve_records": [
            {**record, "reference_utc": record["reference_utc"].isoformat() + "Z", "validity_utc": record["validity_utc"].isoformat() + "Z"}
            for record in sorted_records[:12]
        ],
        "last_twelve_records": [
            {**record, "reference_utc": record["reference_utc"].isoformat() + "Z", "validity_utc": record["validity_utc"].isoformat() + "Z"}
            for record in sorted_records[-12:]
        ],
        "interpretation_limit": "This diagnostic describes GRIB metadata and values only. It does not select an accumulation rule. A reconstruction rule must be fixed only after this output is reviewed against the documented request semantics.",
    }
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: output[key] for key in ["record_count", "unique_validity_count", "duplicate_or_nonunique_validities", "validity_hour_gap_counts", "step_range_counts", "start_step_counts", "end_step_counts", "forecast_time_counts", "step_type_counts", "units"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
