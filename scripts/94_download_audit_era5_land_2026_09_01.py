"""Download and audit the ERA5 Land day required by the fixed 24 h window."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import cdsapi
import eccodes
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
TARGET = ROOT / "data" / "raw" / "era5_land" / "era5_land_core_hourly_2026-09-01_tolima_screening_bbox.grib"
MANIFEST = ROOT / "data" / "raw" / "era5_land" / "era5_land_core_hourly_2026-09-01_manifest.json"
OUTPUT = ROOT / "results" / "exploratory" / "audit_era5_land_core_2026-09-01.json"
GRID_REFERENCE = ROOT / "data" / "raw" / "era5_land" / "era5_land_core_hourly_2026-07_tolima_screening_bbox.grib"
VARIABLES = [
    "2m_temperature",
    "2m_dewpoint_temperature",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "total_precipitation",
]
EXPECTED = {
    "2t": {"units": "K", "step_type": "instant"},
    "2d": {"units": "K", "step_type": "instant"},
    "10u": {"units": "m s**-1", "step_type": "instant"},
    "10v": {"units": "m s**-1", "step_type": "instant"},
    "tp": {"units": "m", "step_type": "accum"},
}


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def grid_signature(message) -> str:
    coordinates = np.column_stack((
        eccodes.codes_get_array(message, "latitudes"),
        eccodes.codes_get_array(message, "longitudes"),
    )).astype("<f8", copy=False)
    return hashlib.sha256(coordinates.tobytes()).hexdigest().upper()


def first_grid_signature(path: Path) -> str:
    with path.open("rb") as stream:
        message = eccodes.codes_grib_new_from_file(stream)
    if message is None:
        raise RuntimeError(f"No GRIB message in {path}")
    try:
        return grid_signature(message)
    finally:
        eccodes.codes_release(message)


def main() -> None:
    for target in (TARGET, MANIFEST, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    credentials = read_env(ENV_FILE)
    request = {
        "variable": VARIABLES,
        "year": ["2026"],
        "month": ["09"],
        "day": ["01"],
        "time": [f"{hour:02d}:00" for hour in range(24)],
        "data_format": "grib",
        "download_format": "unarchived",
        "area": [6.2, -76.2, 2.0, -74.0],
    }
    client = cdsapi.Client(url=credentials["CDS_API_URL"], key=credentials["CDS_API_KEY"], quiet=True, progress=False)
    client.retrieve("reanalysis-era5-land", request).download(str(TARGET))
    MANIFEST.write_text(json.dumps({
        "dataset": "reanalysis-era5-land",
        "request": request,
        "file": str(TARGET.relative_to(ROOT)),
        "bytes": TARGET.stat().st_size,
        "sha256": sha256(TARGET),
        "purpose": "Complete the prespecified first 24 h weather window for events beginning on 2026-08-31.",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    validities = defaultdict(set)
    units = defaultdict(set)
    step_types = defaultdict(set)
    step_ranges = defaultdict(set)
    nonfinite = Counter()
    negatives = Counter()
    signatures = defaultdict(set)
    message_count = 0
    with TARGET.open("rb") as stream:
        while True:
            message = eccodes.codes_grib_new_from_file(stream)
            if message is None:
                break
            try:
                short = eccodes.codes_get(message, "shortName")
                date = str(eccodes.codes_get(message, "validityDate"))
                time = int(eccodes.codes_get(message, "validityTime"))
                validity = datetime.strptime(f"{date}{time:04d}", "%Y%m%d%H%M")
                values = eccodes.codes_get_array(message, "values")
                validities[short].add(validity)
                units[short].add(eccodes.codes_get(message, "units"))
                step_types[short].add(eccodes.codes_get(message, "stepType"))
                step_ranges[short].add(str(eccodes.codes_get(message, "stepRange")))
                nonfinite[short] += int(np.count_nonzero(~np.isfinite(values)))
                negatives[short] += int(np.count_nonzero(values < 0))
                signatures[short].add(grid_signature(message))
                message_count += 1
            finally:
                eccodes.codes_release(message)

    expected_times = {datetime(2026, 9, 1, hour) for hour in range(24)}
    reference_signature = first_grid_signature(GRID_REFERENCE)
    variable_records = {}
    gates = {}
    for short, expected in EXPECTED.items():
        record = {
            "observed_hour_count": len(validities[short]),
            "missing_validity_times": [v.isoformat() for v in sorted(expected_times - validities[short])],
            "unexpected_validity_times": [v.isoformat() for v in sorted(validities[short] - expected_times)],
            "units": sorted(units[short]),
            "step_types": sorted(step_types[short]),
            "step_ranges": sorted(step_ranges[short]),
            "nonfinite_value_count": nonfinite[short],
            "negative_value_count": negatives[short],
            "grid_signature_count": len(signatures[short]),
            "matches_reference_grid": signatures[short] == {reference_signature},
        }
        variable_records[short] = record
        gates[short] = (
            validities[short] == expected_times
            and units[short] == {expected["units"]}
            and step_types[short] == {expected["step_type"]}
            and nonfinite[short] == 0
            and signatures[short] == {reference_signature}
            and (short != "tp" or negatives[short] == 0)
        )
    result = {
        "audit_date": "2026-09-06",
        "source_file": str(TARGET.relative_to(ROOT)),
        "source_sha256": sha256(TARGET),
        "message_count": message_count,
        "expected_message_count": 120,
        "variables": variable_records,
        "variable_gates": gates,
        "all_gates_passed": message_count == 120 and all(gates.values()),
        "precipitation_interpretation": "tp has stepType accum and one-hour step ranges, so each validity value is treated as precipitation for its individual hourly interval, not a progressive daily total.",
        "interpretation_limit": "This audit verifies structure, time, units and numeric completeness. It does not establish local observational accuracy or create study results.",
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"message_count": message_count, "variable_gates": gates, "all_gates_passed": result["all_gates_passed"]}, indent=2))
    if not result["all_gates_passed"]:
        raise RuntimeError("ERA5 Land September 1 audit failed")


if __name__ == "__main__":
    main()
