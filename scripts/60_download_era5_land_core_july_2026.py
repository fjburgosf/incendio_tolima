"""Descarga ERA5 Land horario para julio de 2026 en un archivo crudo."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import cdsapi


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "era5_land" / "era5_land_core_hourly_2026-07_tolima_screening_bbox.grib"
OUTPUT = ROOT / "results" / "exploratory" / "download_era5_land_core_july_2026_manifest.json"
VARIABLES = ["2m_temperature", "2m_dewpoint_temperature", "10m_u_component_of_wind", "10m_v_component_of_wind", "total_precipitation"]


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def main() -> None:
    if RAW.exists() or OUTPUT.exists():
        raise FileExistsError("La descarga o su manifiesto ya existen y no se sobrescribirán")
    credentials = read_env(ROOT / ".env")
    request = {
        "variable": VARIABLES,
        "year": ["2026"],
        "month": ["07"],
        "day": [f"{day:02d}" for day in range(1, 32)],
        "time": [f"{hour:02d}:00" for hour in range(24)],
        "data_format": "grib",
        "download_format": "unarchived",
        "area": [6.2, -76.2, 2.0, -74.0],
    }
    client = cdsapi.Client(url=credentials["CDS_API_URL"], key=credentials["CDS_API_KEY"], quiet=True, progress=False)
    RAW.parent.mkdir(parents=True, exist_ok=True)
    client.retrieve("reanalysis-era5-land", request).download(str(RAW))
    hasher = hashlib.sha256(RAW.read_bytes()).hexdigest().upper()
    result = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": "reanalysis-era5-land",
        "raw_file": str(RAW.relative_to(ROOT)).replace("\\", "/"),
        "bytes": RAW.stat().st_size,
        "sha256": hasher,
        "request": request,
        "interpretation_limit": "Manifiesto de adquisición. La continuidad y estructura se auditan por separado. No contiene resultados analíticos.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
