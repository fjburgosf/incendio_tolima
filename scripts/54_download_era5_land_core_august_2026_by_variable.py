"""Descarga agosto de 2026 por variable para reducir la carga de cada solicitud CDS."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import cdsapi


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "era5_land"
OUTPUT = ROOT / "results" / "exploratory" / "download_era5_land_core_august_2026_by_variable_manifest.json"
VARIABLES = {
    "2m_temperature": "2t",
    "2m_dewpoint_temperature": "2d",
    "10m_u_component_of_wind": "10u",
    "10m_v_component_of_wind": "10v",
    "total_precipitation": "tp",
}


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest().upper()


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("El manifiesto ya existe y no se sobrescribirá")
    credentials = read_env(ROOT / ".env")
    client = cdsapi.Client(url=credentials["CDS_API_URL"], key=credentials["CDS_API_KEY"], quiet=True, progress=False)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    for variable, short_name in VARIABLES.items():
        target = RAW_DIR / f"era5_land_{short_name}_hourly_2026-08_tolima_screening_bbox.grib"
        if target.exists():
            raise FileExistsError(f"El archivo ya existe y no se sobrescribirá: {target.name}")
        request = {
            "variable": [variable],
            "year": ["2026"],
            "month": ["08"],
            "day": [f"{day:02d}" for day in range(1, 32)],
            "time": [f"{hour:02d}:00" for hour in range(24)],
            "data_format": "grib",
            "download_format": "unarchived",
            "area": [6.2, -76.2, 2.0, -74.0],
        }
        client.retrieve("reanalysis-era5-land", request).download(str(target))
        records.append(
            {
                "variable": variable,
                "short_name_expected": short_name,
                "file": str(target.relative_to(ROOT)).replace("\\", "/"),
                "bytes": target.stat().st_size,
                "sha256": sha256(target),
                "request": request,
            }
        )
    result = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": "reanalysis-era5-land",
        "files": records,
        "interpretation_limit": "Manifiesto de adquisición. La continuidad, metadatos GRIB, acumulación de precipitación y derivaciones se auditan en otro script. No contiene resultados analíticos.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "downloaded": records}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
