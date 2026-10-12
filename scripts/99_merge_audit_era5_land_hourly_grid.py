"""Merge audited ERA5 Land hourly fields without altering raw GRIB files."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import eccodes
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "era5_land"
OUTPUT = ROOT / "data" / "interim" / "era5_land_hourly_grid_v1.npz"
AUDIT = ROOT / "results" / "exploratory" / "audit_era5_land_hourly_grid_v1.json"
VARIABLES = ("2t", "2d", "10u", "10v", "tp")
EXPECTED_UNITS = {"2t": "K", "2d": "K", "10u": "m s**-1", "10v": "m s**-1", "tp": "m"}
EXPECTED_STEP_TYPE = {"2t": "instant", "2d": "instant", "10u": "instant", "10v": "instant", "tp": "accum"}
SOURCES = (
    RAW / "era5_land_core_hourly_2026-06_tolima_screening_bbox.grib",
    RAW / "era5_land_core_hourly_2026-07_tolima_screening_bbox.grib",
    RAW / "era5_land_2t_hourly_2026-08_tolima_screening_bbox.grib",
    RAW / "era5_land_2d_hourly_2026-08_tolima_screening_bbox.grib",
    RAW / "era5_land_10u_hourly_2026-08_tolima_screening_bbox.grib",
    RAW / "era5_land_10v_hourly_2026-08_tolima_screening_bbox.grib",
    RAW / "era5_land_tp_hourly_2026-08_tolima_screening_bbox.grib",
    RAW / "era5_land_core_2026-08-31T20-23Z_supplement_check.grib",
    RAW / "era5_land_core_hourly_2026-09-01_tolima_screening_bbox.grib",
)
START = datetime(2026, 6, 1, 0)
END = datetime(2026, 9, 1, 21)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def validity(message) -> datetime:
    date = str(eccodes.codes_get(message, "validityDate"))
    clock = int(eccodes.codes_get(message, "validityTime"))
    return datetime.strptime(f"{date}{clock:04d}", "%Y%m%d%H%M")


def expected_times() -> list[datetime]:
    count = int((END - START).total_seconds() // 3600) + 1
    return [START + timedelta(hours=index) for index in range(count)]


def main() -> None:
    for target in (OUTPUT, AUDIT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    absent = [str(path.relative_to(ROOT)) for path in SOURCES if not path.exists()]
    if absent:
        raise FileNotFoundError(f"Missing ERA5 Land sources: {absent}")
    records: dict[str, dict[datetime, np.ndarray]] = defaultdict(dict)
    units: dict[str, set[str]] = defaultdict(set)
    step_types: dict[str, set[str]] = defaultdict(set)
    step_ranges: dict[str, set[str]] = defaultdict(set)
    source_by_variable_time: dict[str, dict[datetime, str]] = defaultdict(dict)
    reference_latitudes: np.ndarray | None = None
    reference_longitudes: np.ndarray | None = None
    nonfinite_count: dict[str, int] = defaultdict(int)
    message_count = 0
    duplicate_keys = []
    unexpected_variables = set()
    for source in SOURCES:
        with source.open("rb") as stream:
            while True:
                message = eccodes.codes_grib_new_from_file(stream)
                if message is None:
                    break
                try:
                    short = str(eccodes.codes_get(message, "shortName"))
                    when = validity(message)
                    if short not in VARIABLES:
                        unexpected_variables.add(short)
                        continue
                    values = np.asarray(eccodes.codes_get_array(message, "values"), dtype=np.float32)
                    latitudes = np.asarray(eccodes.codes_get_array(message, "latitudes"), dtype=np.float64)
                    longitudes = np.asarray(eccodes.codes_get_array(message, "longitudes"), dtype=np.float64)
                    if reference_latitudes is None:
                        reference_latitudes, reference_longitudes = latitudes, longitudes
                    elif not (np.array_equal(latitudes, reference_latitudes) and np.array_equal(longitudes, reference_longitudes)):
                        raise RuntimeError(f"Grid differs in {source.name} at {when.isoformat()} for {short}")
                    if when in records[short]:
                        duplicate_keys.append({"variable": short, "validity_utc": when.isoformat(), "first_source": source_by_variable_time[short][when], "duplicate_source": source.name})
                        continue
                    records[short][when] = values
                    source_by_variable_time[short][when] = source.name
                    units[short].add(str(eccodes.codes_get(message, "units")))
                    step_types[short].add(str(eccodes.codes_get(message, "stepType")))
                    step_ranges[short].add(str(eccodes.codes_get(message, "stepRange")))
                    nonfinite_count[short] += int(np.count_nonzero(~np.isfinite(values)))
                    message_count += 1
                finally:
                    eccodes.codes_release(message)
    if duplicate_keys:
        raise RuntimeError(f"Duplicate variable time records detected: {duplicate_keys[:3]}")
    if unexpected_variables:
        raise RuntimeError(f"Unexpected variables encountered: {sorted(unexpected_variables)}")
    times = expected_times()
    fields = {}
    variable_audit = {}
    for short in VARIABLES:
        observed = set(records[short])
        missing = sorted(set(times) - observed)
        unexpected = sorted(observed - set(times))
        unit_ok = units[short] == {EXPECTED_UNITS[short]}
        step_type_ok = step_types[short] == {EXPECTED_STEP_TYPE[short]}
        if missing or unexpected or not unit_ok or not step_type_ok or nonfinite_count[short] != 0:
            raise RuntimeError(f"ERA5 merge gate failed for {short}: missing={len(missing)} unexpected={len(unexpected)} units={units[short]} step_types={step_types[short]} nonfinite={nonfinite_count[short]}")
        fields[short] = np.stack([records[short][when] for when in times], axis=0)
        variable_audit[short] = {
            "hour_count": len(observed),
            "units": sorted(units[short]),
            "step_types": sorted(step_types[short]),
            "step_ranges": sorted(step_ranges[short]),
            "nonfinite_value_count": nonfinite_count[short],
            "minimum": float(fields[short].min()),
            "maximum": float(fields[short].max()),
        }
    if reference_latitudes is None or reference_longitudes is None:
        raise RuntimeError("No ERA5 messages read")
    np.savez_compressed(
        OUTPUT,
        validity_utc=np.asarray([when.strftime("%Y-%m-%dT%H:%M:%SZ") for when in times]),
        latitudes=reference_latitudes,
        longitudes=reference_longitudes,
        **fields,
    )
    source_hashes = [{"file": str(path.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(path), "bytes": path.stat().st_size} for path in SOURCES]
    audit = {
        "time_range_utc": {"start": times[0].isoformat() + "Z", "end": times[-1].isoformat() + "Z", "hour_count": len(times)},
        "grid_point_count": int(reference_latitudes.size),
        "source_files": source_hashes,
        "message_count": message_count,
        "expected_message_count": len(times) * len(VARIABLES),
        "variables": variable_audit,
        "output": {"file": str(OUTPUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(OUTPUT), "bytes": OUTPUT.stat().st_size},
        "precipitation_handling": "tp values retain their GRIB hourly validity times and are not summed in this merge. They have stepType accum and documented one-hour step ranges in the source audit.",
        "interpretation_limit": "The merged file is a lossless reorganization of audited ERA5 Land grid messages for reproducible interpolation. It is not an observational validation or a study result.",
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"message_count": message_count, "time_hours": len(times), "grid_points": int(reference_latitudes.size), "output_bytes": OUTPUT.stat().st_size, "variables": variable_audit}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
