"""Reconstruct ERA5 Land hourly precipitation by within-cycle differencing after diagnosis."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import eccodes
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "precipitation_reconstruction_and_exploratory_analysis_plan_v2.json"
RAW = ROOT / "data" / "raw" / "era5_land"
SOURCES = [
    RAW / "era5_land_core_hourly_2026-06_tolima_screening_bbox.grib",
    RAW / "era5_land_core_hourly_2026-07_tolima_screening_bbox.grib",
    RAW / "era5_land_tp_hourly_2026-08_tolima_screening_bbox.grib",
    RAW / "era5_land_core_2026-08-31T20-23Z_supplement_check.grib",
    RAW / "era5_land_core_hourly_2026-09-01_tolima_screening_bbox.grib",
]
OUT = ROOT / "data" / "interim" / "era5_land_hourly_precipitation_increment_v3.npz"
AUDIT = ROOT / "results" / "exploratory" / "audit_era5_land_hourly_precipitation_increment_v3.json"
START = datetime(2026, 6, 1, 1)
END = datetime(2026, 9, 1, 21)
ROUNDING_TOLERANCE_M = 0.0000001


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
    cycles: dict[datetime, dict[int, tuple[datetime, np.ndarray]]] = {}
    latitudes: np.ndarray | None = None
    longitudes: np.ndarray | None = None
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
                    reference = parse_datetime(int(eccodes.codes_get(message, "dataDate")), int(eccodes.codes_get(message, "dataTime")))
                    valid = parse_datetime(int(eccodes.codes_get(message, "validityDate")), int(eccodes.codes_get(message, "validityTime")))
                    start_step = int(eccodes.codes_get(message, "startStep"))
                    end_step = int(eccodes.codes_get(message, "endStep"))
                    if end_step - start_step != 1:
                        raise RuntimeError(f"Non-hourly precipitation step {start_step} to {end_step} in {source.name}")
                    values = np.asarray(eccodes.codes_get_array(message, "values"), dtype=np.float64)
                    current_latitudes = np.asarray(eccodes.codes_get_array(message, "latitudes"), dtype=np.float64)
                    current_longitudes = np.asarray(eccodes.codes_get_array(message, "longitudes"), dtype=np.float64)
                    if latitudes is None:
                        latitudes, longitudes = current_latitudes, current_longitudes
                    elif not (np.array_equal(latitudes, current_latitudes) and np.array_equal(longitudes, current_longitudes)):
                        raise RuntimeError("Precipitation grid differs among source records")
                    if reference not in cycles:
                        cycles[reference] = {}
                    if end_step in cycles[reference]:
                        raise RuntimeError(f"Duplicate reference-step record {reference.isoformat()} {end_step}")
                    cycles[reference][end_step] = (valid, values)
                    count += 1
                finally:
                    eccodes.codes_release(message)
        source_counts[source.name] = count
    increments: dict[datetime, np.ndarray] = {}
    negative_clipped = 0
    negative_below_tolerance = 0
    boundary_omitted: list[str] = []
    for reference, records in sorted(cycles.items()):
        for end_step, (valid, value) in sorted(records.items()):
            if end_step == 1:
                increment = value
            elif end_step - 1 in records:
                increment = value - records[end_step - 1][1]
            else:
                boundary_omitted.append(valid.isoformat() + "Z")
                continue
            if valid in increments:
                raise RuntimeError(f"Duplicate reconstructed validity time {valid.isoformat()}")
            negative_below_tolerance += int(np.count_nonzero(increment < -ROUNDING_TOLERANCE_M))
            negative_clipped += int(np.count_nonzero((increment < 0) & (increment >= -ROUNDING_TOLERANCE_M)))
            increments[valid] = np.maximum(increment, 0.0).astype(np.float32)
    times = expected_times()
    missing = sorted(set(times) - set(increments))
    unexpected = sorted(set(increments) - set(times))
    if missing or unexpected or negative_below_tolerance or latitudes is None or longitudes is None:
        raise RuntimeError(f"Precipitation reconstruction gate failed missing={len(missing)} unexpected={len(unexpected)} below_tolerance={negative_below_tolerance}")
    values = np.stack([increments[time] for time in times], axis=0)
    if values.shape != (len(times), 989) or not np.isfinite(values).all():
        raise RuntimeError("Unexpected increment array dimensions or nonfinite values")
    np.savez_compressed(
        OUT,
        validity_utc=np.asarray([time.strftime("%Y-%m-%dT%H:%M:%SZ") for time in times]),
        latitudes=latitudes,
        longitudes=longitudes,
        tp_increment_m=values,
    )
    audit = {
        "plan": {"file": str(PLAN.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(PLAN)},
        "source_files": [{"file": str(path.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(path)} for path in SOURCES],
        "derivation": "Within each common forecast-reference cycle, subtract the value ending at the preceding step. Use the first step directly. Omit the single earliest boundary value that lacks its preceding step in the downloaded window.",
        "retracted_direct_value_output": "data/interim/era5_land_hourly_precipitation_interval_v2.npz is retained but prohibited from analysis because it directly summed cumulatively increasing records.",
        "hour_count": len(times),
        "time_range_utc": {"start": times[0].isoformat() + "Z", "end": times[-1].isoformat() + "Z"},
        "grid_point_count": int(latitudes.size),
        "source_record_counts": source_counts,
        "boundary_omitted_validity_times": boundary_omitted,
        "negative_rounding_values_clipped": negative_clipped,
        "negative_values_below_tolerance": negative_below_tolerance,
        "minimum_increment_m": float(values.min()),
        "maximum_increment_m": float(values.max()),
        "output": {"file": str(OUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(OUT)},
        "interpretation_limit": "The output is a reorganization of ERA5 Land accumulated precipitation. It remains a gridded reanalysis estimate, not a point-gauge measurement or evidence of rainfall at the ignition site.",
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"hour_count": len(times), "negative_rounding_values_clipped": negative_clipped, "maximum_increment_m": float(values.max()), "audit": str(AUDIT.relative_to(ROOT))}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
