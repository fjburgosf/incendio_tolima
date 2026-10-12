"""Verify one common ERA5-Land weather snapshot across historical Augusts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cdsapi
from eccodes import codes_get, codes_grib_new_from_file, codes_release


ROOT = Path(__file__).resolve().parents[1]
ACCESS_DATE = "2026-09-07"
ENV_FILE = ROOT / ".env"
RAW_DIR = ROOT / "data" / "raw" / "era5_land"
OUTPUT = RAW_DIR / "era5_land_historical_weather_access_2018_2020_2022_2024_2025_2026_08_15T00.grib"
MANIFEST = RAW_DIR / "era5_land_historical_weather_access_2026-09-07_manifest.json"
AUDIT = ROOT / "results" / "exploratory" / "audit_era5_land_historical_weather_access_2026-09-07.json"
YEARS = ("2018", "2020", "2022", "2024", "2025", "2026")
VARIABLES = (
    "2m_temperature",
    "2m_dewpoint_temperature",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
)
REQUEST = {
    "variable": list(VARIABLES),
    "year": list(YEARS),
    "month": ["08"],
    "day": ["15"],
    "time": ["00:00"],
    "data_format": "grib",
    "download_format": "unarchived",
    "area": [6.2, -76.2, 2.0, -74.0],
}


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def inspect_grib(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with path.open("rb") as handle:
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
    return records


def main() -> None:
    for target in (OUTPUT, MANIFEST, AUDIT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {target}")
    values = read_env(ENV_FILE)
    client = cdsapi.Client(
        url=values["CDS_API_URL"], key=values["CDS_API_KEY"], quiet=True, progress=False
    )
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    client.retrieve("reanalysis-era5-land", REQUEST).download(str(OUTPUT))
    records = inspect_grib(OUTPUT)
    observed = {(str(record["validity_date"]), str(record["short_name"])) for record in records}
    expected = {(f"{year}0815", short_name) for year in YEARS for short_name in ("2t", "2d", "10u", "10v")}
    missing = sorted(expected - observed)
    unexpected = sorted(observed - expected)
    manifest = {
        "dataset": "reanalysis-era5-land",
        "request": REQUEST,
        "file": OUTPUT.name,
        "bytes": OUTPUT.stat().st_size,
        "sha256": sha256(OUTPUT),
        "credential_handling": "Credentials read from .env and not recorded in this manifest.",
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    audit = {
        "audit_date": ACCESS_DATE,
        "inference_class": "descriptive source-access audit",
        "input_manifest": str(MANIFEST.relative_to(ROOT)),
        "input_sha256": manifest["sha256"],
        "record_count": len(records),
        "records": records,
        "expected_record_count": len(expected),
        "missing_expected_year_variable_pairs": missing,
        "unexpected_year_variable_pairs": unexpected,
        "interpretation": (
            "A common valid-time snapshot establishes technical retrieval and schema access. "
            "It does not establish temporal aggregation, local accuracy, station agreement, "
            "or the suitability of a specific meteorological exposure window."
        ),
    }
    AUDIT.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(audit, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
