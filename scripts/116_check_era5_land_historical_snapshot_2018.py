"""Download one 2018 ERA5-Land weather snapshot to isolate historical access."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cdsapi
from eccodes import codes_get, codes_grib_new_from_file, codes_release


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
RAW_DIR = ROOT / "data" / "raw" / "era5_land"
OUTPUT = RAW_DIR / "era5_land_historical_access_2018_08_15T00.grib"
MANIFEST = RAW_DIR / "era5_land_historical_access_2018_08_15T00_manifest.json"
AUDIT = ROOT / "results" / "exploratory" / "check_era5_land_historical_access_2018_2026-09-07.json"
REQUEST = {
    "variable": ["2m_temperature", "2m_dewpoint_temperature", "10m_u_component_of_wind", "10m_v_component_of_wind"],
    "year": ["2018"],
    "month": ["08"],
    "day": ["15"],
    "time": ["00:00"],
    "data_format": "grib",
    "download_format": "unarchived",
    "area": [6.2, -76.2, 2.0, -74.0],
}


def env_values() -> dict[str, str]:
    return {
        key.strip(): value.strip()
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
        for key, value in [line.split("=", 1)]
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def inspect(path: Path) -> list[dict[str, object]]:
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
    credentials = env_values()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    print("Submitting one historical ERA5-Land request", flush=True)
    client = cdsapi.Client(url=credentials["CDS_API_URL"], key=credentials["CDS_API_KEY"], quiet=True, progress=False)
    client.retrieve("reanalysis-era5-land", REQUEST).download(str(OUTPUT))
    records = inspect(OUTPUT)
    manifest = {"dataset": "reanalysis-era5-land", "request": REQUEST, "file": OUTPUT.name, "bytes": OUTPUT.stat().st_size, "sha256": sha256(OUTPUT)}
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    audit = {"request": REQUEST, "record_count": len(records), "records": records, "raw_manifest": str(MANIFEST.relative_to(ROOT)), "interpretation": "One snapshot verifies source access and schema only."}
    AUDIT.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
