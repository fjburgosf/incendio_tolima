"""Create audited hourly ERA5 Land precipitation intervals from one-hour GRIB steps."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import eccodes
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "precipitation_reconstruction_and_exploratory_analysis_plan_v1.json"
RAW = ROOT / "data" / "raw" / "era5_land"
SOURCES = [
    RAW / "era5_land_core_hourly_2026-06_tolima_screening_bbox.grib",
    RAW / "era5_land_core_hourly_2026-07_tolima_screening_bbox.grib",
    RAW / "era5_land_tp_hourly_2026-08_tolima_screening_bbox.grib",
    RAW / "era5_land_core_2026-08-31T20-23Z_supplement_check.grib",
    RAW / "era5_land_core_hourly_2026-09-01_tolima_screening_bbox.grib",
]
OUT = ROOT / "data" / "interim" / "era5_land_hourly_precipitation_interval_v2.npz"
AUDIT = ROOT / "results" / "exploratory" / "audit_era5_land_hourly_precipitation_interval_v2.json"
START = datetime(2026, 6, 1, 0)
END = datetime(2026, 9, 1, 21)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def parse_datetime(date: int, time: int) -> datetime:
    return datetime.strptime(f"{date}{time:04d}", "%Y%m%d%H%M")


def expected_times() -> list[datetime]:
    count = int((END - START).total_seconds() // 3600) + 1
    return [START + timedelta(hours=index) for index in range(count)]


def main() -> None:
    for path in (OUT, AUDIT):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {path}")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    records: dict[datetime, np.ndarray] = {}
    latitudes: np.ndarray | None = None
    longitudes: np.ndarray | None = None
    step_ranges: set[str] = set()
    negative_values = 0
    source_counts: dict[str, int] = {}
    for source in SOURCES:
        count = 0
        with source.open("rb") as handle:
            while message := eccodes.codes_grib_new_from_file(handle):
                try:
                    if str(eccodes.codes_get(message, "shortName")) != "tp":
                        continue
                    if str(eccodes.codes_get(message, "stepType")) != "accum" or str(eccodes.codes_get(message, "units")) != "m":
                        raise RuntimeError(f"Unexpected precipitation metadata in {source.name}")
                    start_step = int(eccodes.codes_get(message, "startStep"))
                    end_step = int(eccodes.codes_get(message, "endStep"))
                    if end_step - start_step != 1:
                        raise RuntimeError(f"Non-hourly precipitation step {start_step} to {end_step} in {source.name}")
                    valid = parse_datetime(int(eccodes.codes_get(message, "validityDate")), int(eccodes.codes_get(message, "validityTime")))
                    if valid in records:
                        raise RuntimeError(f"Duplicate precipitation validity time {valid.isoformat()}")
                    values = np.asarray(eccodes.codes_get_array(message, "values"), dtype=np.float32)
                    if np.any(values < -1e-10):
                        negative_values += int(np.count_nonzero(values < -1e-10))
                    current_latitudes = np.asarray(eccodes.codes_get_array(message, "latitudes"), dtype=np.float64)
                    current_longitudes = np.asarray(eccodes.codes_get_array(message, "longitudes"), dtype=np.float64)
                    if latitudes is None:
                        latitudes, longitudes = current_latitudes, current_longitudes
                    elif not (np.array_equal(latitudes, current_latitudes) and np.array_equal(longitudes, current_longitudes)):
                        raise RuntimeError("Precipitation grid differs among source records")
                    records[valid] = values
                    step_ranges.add(str(eccodes.codes_get(message, "stepRange")))
                    count += 1
                finally:
                    eccodes.codes_release(message)
        source_counts[source.name] = count
    times = expected_times()
    missing = sorted(set(times) - set(records))
    unexpected = sorted(set(records) - set(times))
    if missing or unexpected or negative_values or latitudes is None or longitudes is None:
        raise RuntimeError(f"Precipitation reconstruction gate failed missing={len(missing)} unexpected={len(unexpected)} negatives={negative_values}")
    intervals_m = np.stack([records[time] for time in times], axis=0)
    if intervals_m.shape != (len(times), 989) or not np.isfinite(intervals_m).all():
        raise RuntimeError("Unexpected interval array dimensions or nonfinite precipitation")
    np.savez_compressed(
        OUT,
        validity_utc=np.asarray([time.strftime("%Y-%m-%dT%H:%M:%SZ") for time in times]),
        latitudes=latitudes,
        longitudes=longitudes,
        tp_interval_m=intervals_m,
    )
    audit = {
        "plan": {"file": str(PLAN.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(PLAN)},
        "source_files": [{"file": str(path.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(path)} for path in SOURCES],
        "step_interpretation": "Each source record is a one-hour accumulated interval because its GRIB step range has width one hour. No subtraction between adjacent records is applied.",
        "retracted_implementation": "The earlier v1 subtraction implementation failed because it subtracted values across different one-hour intervals. It produced negative pseudo-increments and no output. It is not used.",
        "hour_count": len(times),
        "time_range_utc": {"start": times[0].isoformat() + "Z", "end": times[-1].isoformat() + "Z"},
        "grid_point_count": int(latitudes.size),
        "source_record_counts": source_counts,
        "unique_step_ranges": sorted(step_ranges),
        "negative_value_count": negative_values,
        "minimum_interval_m": float(intervals_m.min()),
        "maximum_interval_m": float(intervals_m.max()),
        "output": {"file": str(OUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(OUT)},
        "interpretation_limit": "The output is a reorganization of ERA5 Land one-hour precipitation intervals. It is a gridded reanalysis estimate and not a gauge measurement, local calibration, physical rainfall truth, or an event-level outcome.",
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"hour_count": len(times), "grid_point_count": int(latitudes.size), "minimum_interval_m": float(intervals_m.min()), "maximum_interval_m": float(intervals_m.max()), "audit": str(AUDIT.relative_to(ROOT))}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
