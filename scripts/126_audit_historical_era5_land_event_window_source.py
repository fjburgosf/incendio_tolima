"""Audit structure of the pre-specified historical ERA5-Land source files."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from eccodes import codes_get, codes_get_array, codes_grib_new_from_file, codes_release


ROOT = Path(__file__).resolve().parents[1]
ACCESS_DATE = "2026-09-07"
PLAN = ROOT / "configs" / "historical_era5_land_event_window_source_plan_v1.json"
RAW_DIR = ROOT / "data" / "raw" / "era5_land"
MANIFEST = RAW_DIR / f"era5_land_historical_event_window_source_{ACCESS_DATE}_manifest_v2.json"
OUTPUT = ROOT / "results" / "exploratory" / f"audit_historical_era5_land_event_window_source_{ACCESS_DATE}.json"
EXPECTED_UNITS = {"2t": "K", "2d": "K", "10u": "m s**-1", "10v": "m s**-1"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def expected_datetimes(year: int, period: str) -> set[tuple[int, int]]:
    start = datetime(year, 8, 1) if period == "august" else datetime(year, 9, 1)
    count = 31 * 24 if period == "august" else 24
    return {
        (int((start + timedelta(hours=offset)).strftime("%Y%m%d")), int((start + timedelta(hours=offset)).strftime("%H%M")))
        for offset in range(count)
    }


def audit_file(path: Path, year: int, period: str) -> dict[str, object]:
    records: list[dict[str, object]] = []
    with path.open("rb") as handle:
        while message := codes_grib_new_from_file(handle):
            try:
                values = codes_get_array(message, "values")
                records.append(
                    {
                        "short_name": codes_get(message, "shortName"),
                        "units": codes_get(message, "units"),
                        "validity_date": int(codes_get(message, "validityDate")),
                        "validity_time": int(codes_get(message, "validityTime")),
                        "number_of_values": int(codes_get(message, "numberOfValues")),
                        "minimum": float(values.min()),
                        "maximum": float(values.max()),
                    }
                )
            finally:
                codes_release(message)
    expected_times = expected_datetimes(year, period)
    by_variable: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in records:
        by_variable[str(record["short_name"])].append(record)
    variable_audit = {}
    for short_name in EXPECTED_UNITS:
        values = by_variable.get(short_name, [])
        dates = {(record["validity_date"], record["validity_time"]) for record in values}
        variable_audit[short_name] = {
            "message_count": len(values),
            "unique_valid_times": len(dates),
            "missing_valid_times": len(expected_times - dates),
            "unexpected_valid_times": len(dates - expected_times),
            "duplicate_valid_time_messages": len(values) - len(dates),
            "units": sorted({str(record["units"]) for record in values}),
            "number_of_values": sorted({int(record["number_of_values"]) for record in values}),
            "minimum": min((float(record["minimum"]) for record in values), default=None),
            "maximum": max((float(record["maximum"]) for record in values), default=None),
        }
    expected_count = len(expected_times) * len(EXPECTED_UNITS)
    return {
        "file": str(path.relative_to(ROOT)).replace("\\", "/"),
        "sha256": sha256(path),
        "year": year,
        "period": period,
        "message_count": len(records),
        "expected_message_count": expected_count,
        "unexpected_short_names": sorted(set(by_variable) - set(EXPECTED_UNITS)),
        "variable_audit": variable_audit,
    }


def file_passes(record: dict[str, object]) -> bool:
    if record["message_count"] != record["expected_message_count"] or record["unexpected_short_names"]:
        return False
    for short_name, expected_unit in EXPECTED_UNITS.items():
        details = record["variable_audit"][short_name]
        if (
            details["message_count"] != details["unique_valid_times"]
            or details["missing_valid_times"]
            or details["unexpected_valid_times"]
            or details["duplicate_valid_time_messages"]
            or details["units"] != [expected_unit]
            or len(details["number_of_values"]) != 1
        ):
            return False
    return True


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {OUTPUT}")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    files = []
    for entry in manifest["files"]:
        path = ROOT / entry["file"]
        if not path.exists():
            raise FileNotFoundError(f"Manifest file missing: {path}")
        if sha256(path) != entry["sha256"]:
            raise RuntimeError(f"Manifest hash mismatch: {path.name}")
        files.append(audit_file(path, int(entry["year"]), str(entry["period"])))
    expected_pairs = {(year, period) for year in plan["years"] for period in ("august", "september_extension")}
    observed_pairs = {(record["year"], record["period"]) for record in files}
    grid_sizes = {
        next(iter(details["number_of_values"]))
        for record in files
        for details in record["variable_audit"].values()
        if len(details["number_of_values"]) == 1
    }
    passed = observed_pairs == expected_pairs and len(files) == len(expected_pairs) and all(file_passes(record) for record in files) and len(grid_sizes) == 1
    output = {
        "audit_date": ACCESS_DATE,
        "inference_class": "descriptive source-structure audit",
        "plan": str(PLAN.relative_to(ROOT)),
        "manifest": str(MANIFEST.relative_to(ROOT)),
        "manifest_sha256": sha256(MANIFEST),
        "expected_file_pairs": sorted([list(pair) for pair in expected_pairs]),
        "observed_file_pairs": sorted([list(pair) for pair in observed_pairs]),
        "common_grid_value_count": list(grid_sizes)[0] if len(grid_sizes) == 1 else None,
        "files": sorted(files, key=lambda record: (record["year"], record["period"])),
        "acceptance_result": "pass" if passed else "fail",
        "interpretation_limit": "This audit verifies requested temporal coverage, variable metadata and shared grid size. It does not construct event features, demonstrate local accuracy or support historical associations.",
    }
    OUTPUT.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "acceptance_result": output["acceptance_result"], "files": len(files)}, ensure_ascii=False, indent=2))
    if not passed:
        raise RuntimeError("Historical ERA5-Land event-window source audit failed")


if __name__ == "__main__":
    main()
