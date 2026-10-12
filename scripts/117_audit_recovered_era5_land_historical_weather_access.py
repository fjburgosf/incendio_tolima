"""Audit the raw historical ERA5-Land file recovered after terminal timeout."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from eccodes import codes_get, codes_grib_new_from_file, codes_release


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "era5_land" / "era5_land_historical_weather_access_2018_2020_2022_2024_2025_2026_08_15T00.grib"
MANIFEST = ROOT / "data" / "raw" / "era5_land" / "era5_land_historical_weather_access_2026-09-07_manifest_recovered.json"
OUTPUT = ROOT / "results" / "exploratory" / "audit_recovered_era5_land_historical_weather_access_2026-09-07.json"
YEARS = (2018, 2020, 2022, 2024, 2025, 2026)
EXPECTED_SHORT_NAMES = ("2t", "2d", "10u", "10v")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def main() -> None:
    if not RAW.exists():
        raise FileNotFoundError(RAW)
    for target in (MANIFEST, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {target}")
    records: list[dict[str, object]] = []
    with RAW.open("rb") as handle:
        while message := codes_grib_new_from_file(handle):
            try:
                records.append(
                    {
                        "short_name": codes_get(message, "shortName"),
                        "units": codes_get(message, "units"),
                        "validity_date": codes_get(message, "validityDate"),
                        "validity_time": codes_get(message, "validityTime"),
                        "number_of_values": codes_get(message, "numberOfValues"),
                    }
                )
            finally:
                codes_release(message)
    observed = {(int(record["validity_date"]), str(record["short_name"])) for record in records}
    expected = {(year * 10000 + 815, short_name) for year in YEARS for short_name in EXPECTED_SHORT_NAMES}
    manifest = {
        "file": RAW.name,
        "bytes": RAW.stat().st_size,
        "sha256": sha256(RAW),
        "recovery_note": "The GRIB existed after an interrupted combined access script. This script audits it without a second network request.",
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    audit = {
        "audit_date": "2026-09-07",
        "inference_class": "descriptive source-access audit",
        "raw_manifest": str(MANIFEST.relative_to(ROOT)),
        "record_count": len(records),
        "expected_record_count": len(expected),
        "missing_year_variable_pairs": sorted(expected - observed),
        "unexpected_year_variable_pairs": sorted(observed - expected),
        "records": records,
        "interpretation": "This verifies common historical snapshot access and schema only.",
    }
    OUTPUT.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
