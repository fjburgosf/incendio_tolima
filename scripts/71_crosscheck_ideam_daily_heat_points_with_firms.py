"""Cross-check IDEAM NOAA 20 and NOAA 21 heat points against raw FIRMS NRT."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IDEAM_TOLIMA = ROOT / "data" / "interim" / "ideam_daily_heat_points_tolima_august_2026.csv"
FIRMS_DIR = ROOT / "data" / "raw" / "firms"
FIRMS_MANIFEST = FIRMS_DIR / "download_manifest_august_2026.json"
OUTPUT = ROOT / "results" / "exploratory" / "crosscheck_ideam_daily_heat_points_with_firms_august_2026.json"

IDEAM_TO_FIRMS_SATELLITE = {
    "VIIRS-NOAA-20": "N20",
    "VIIRS-NOAA-21": "N21",
}
CONFIDENCE_MAP = {"Nominal": "n", "Alta": "h", "Baja": "l", "High": "h", "Low": "l"}


def float_or_none(value: str) -> float | None:
    return float(value) if value.strip() else None


def ideam_signature(row: dict[str, str]) -> tuple[str, str, float, float]:
    local_datetime = datetime.strptime(row["datetime_local_utc_minus_5"], "%Y-%m-%d %H:%M")
    utc_datetime = local_datetime + timedelta(hours=5)
    return (
        IDEAM_TO_FIRMS_SATELLITE[row["source"]],
        utc_datetime.strftime("%Y-%m-%d %H:%M"),
        round(float(row["latitude"]), 5),
        round(float(row["longitude"]), 5),
    )


def firms_signature(row: dict[str, str]) -> tuple[str, str, float, float]:
    time_value = str(row["acq_time"]).zfill(4)
    utc_datetime = datetime.strptime(
        f"{row['acq_date']} {time_value}", "%Y-%m-%d %H%M"
    )
    return (
        row["satellite"],
        utc_datetime.strftime("%Y-%m-%d %H:%M"),
        round(float(row["latitude"]), 5),
        round(float(row["longitude"]), 5),
    )


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("El resultado ya existe y no se sobrescribirá")

    with IDEAM_TOLIMA.open(encoding="utf-8-sig", newline="") as handle:
        ideam_rows = [
            row for row in csv.DictReader(handle) if row["source"] in IDEAM_TO_FIRMS_SATELLITE
        ]

    manifest = json.loads(FIRMS_MANIFEST.read_text(encoding="utf-8"))
    firms_rows: list[dict[str, str]] = []
    for entry in manifest["entries"]:
        with (FIRMS_DIR / entry["file"]).open(encoding="utf-8-sig", newline="") as handle:
            firms_rows.extend(csv.DictReader(handle))

    firms_by_signature: dict[tuple[str, str, float, float], list[dict[str, str]]] = defaultdict(list)
    for row in firms_rows:
        firms_by_signature[firms_signature(row)].append(row)

    ideam_signature_counts = Counter(ideam_signature(row) for row in ideam_rows)
    matched = 0
    unmatched = []
    ambiguous = []
    confidence_mismatches = 0
    frp_differences = []
    temperature_differences = []
    alternate_temperature_differences = []
    scan_differences = []
    track_differences = []

    for ideam_row in ideam_rows:
        signature = ideam_signature(ideam_row)
        candidates = firms_by_signature.get(signature, [])
        if not candidates:
            unmatched.append(signature)
            continue
        if len(candidates) > 1:
            ambiguous.append({"signature": signature, "candidate_count": len(candidates)})
            continue
        matched += 1
        firms_row = candidates[0]
        normalized_confidence = CONFIDENCE_MAP.get(
            ideam_row["confidence"], ideam_row["confidence"].strip().lower()[:1]
        )
        confidence_mismatches += normalized_confidence != firms_row["confidence"]

        ideam_frp = float_or_none(ideam_row["thermal_radiation_mw"])
        if ideam_frp is not None:
            frp_differences.append(abs(ideam_frp - float(firms_row["frp"])))
        ideam_temperature = float_or_none(ideam_row["temperature_c"])
        if ideam_temperature is not None:
            temperature_differences.append(
                abs(ideam_temperature - (float(firms_row["bright_ti4"]) - 273.15))
            )
        ideam_alt_temperature = float_or_none(ideam_row["alternate_temperature_c"])
        if ideam_alt_temperature is not None:
            alternate_temperature_differences.append(
                abs(ideam_alt_temperature - (float(firms_row["bright_ti5"]) - 273.15))
            )
        scan_differences.append(abs(float(ideam_row["scan_km"]) - float(firms_row["scan"])))
        track_differences.append(abs(float(ideam_row["track_km"]) - float(firms_row["track"])))

    matched_signatures = set(ideam_signature_counts).intersection(firms_by_signature)
    unmatched_firms_rows = sum(
        len(rows) for signature, rows in firms_by_signature.items() if signature not in matched_signatures
    )
    result = {
        "ideam_source": str(IDEAM_TOLIMA.relative_to(ROOT)).replace("\\", "/"),
        "firms_manifest": str(FIRMS_MANIFEST.relative_to(ROOT)).replace("\\", "/"),
        "ideam_noaa20_noaa21_row_count": len(ideam_rows),
        "ideam_unique_signature_count": len(ideam_signature_counts),
        "ideam_duplicate_signature_rows": len(ideam_rows) - len(ideam_signature_counts),
        "firms_noaa20_noaa21_row_count_in_screening_rectangle": len(firms_rows),
        "firms_unique_signature_count": len(firms_by_signature),
        "firms_duplicate_signature_rows": len(firms_rows) - len(firms_by_signature),
        "ideam_rows_with_single_exact_firms_match": matched,
        "ideam_rows_without_exact_firms_match": len(unmatched),
        "ideam_rows_with_ambiguous_firms_match": len(ambiguous),
        "firms_rows_not_in_ideam_tolima_subset": unmatched_firms_rows,
        "confidence_mismatch_count": confidence_mismatches,
        "maximum_absolute_frp_difference_mw": max(frp_differences) if frp_differences else None,
        "maximum_absolute_temperature_conversion_difference_c": (
            max(temperature_differences) if temperature_differences else None
        ),
        "maximum_absolute_alternate_temperature_conversion_difference_c": (
            max(alternate_temperature_differences) if alternate_temperature_differences else None
        ),
        "maximum_absolute_scan_difference_km": max(scan_differences) if scan_differences else None,
        "maximum_absolute_track_difference_km": max(track_differences) if track_differences else None,
        "time_conversion_rule": "IDEAM local Colombia time plus five hours equals FIRMS UTC time",
        "match_signature": "satellite, UTC minute, latitude rounded to 5 decimals, longitude rounded to 5 decimals",
        "interpretation_limit": (
            "An exact cross-source match demonstrates that the IDEAM NOAA 20 and NOAA 21 rows are "
            "a transformed subset of the audited FIRMS detections. It is not independent validation "
            "of fire occurrence. Temperature and FRP checks assess documented conversion and rounding, "
            "not physical accuracy."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
