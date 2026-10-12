"""Comprueba las variables meteorológicas centrales ERA5 Land del 31 de agosto."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cdsapi
import eccodes


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "era5_land" / "era5_land_core_variables_2026-08-31T00_control.grib"
OUTPUT = ROOT / "results" / "exploratory" / "check_era5_land_core_variables_2026_08_31.json"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest().upper()


def main() -> None:
    if RAW.exists() or OUTPUT.exists():
        raise FileExistsError("El control ya existe y no se sobrescribirá")
    credentials = read_env(ROOT / ".env")
    client = cdsapi.Client(url=credentials["CDS_API_URL"], key=credentials["CDS_API_KEY"], quiet=True, progress=False)
    request = {
        "variable": [
            "2m_temperature",
            "2m_dewpoint_temperature",
            "10m_u_component_of_wind",
            "10m_v_component_of_wind",
            "total_precipitation",
        ],
        "year": ["2026"],
        "month": ["08"],
        "day": ["31"],
        "time": ["00:00"],
        "data_format": "grib",
        "download_format": "unarchived",
        "area": [6.2, -76.2, 2.0, -74.0],
    }
    RAW.parent.mkdir(parents=True, exist_ok=True)
    client.retrieve("reanalysis-era5-land", request).download(str(RAW))
    messages: list[dict[str, object]] = []
    with RAW.open("rb") as stream:
        while True:
            message = eccodes.codes_grib_new_from_file(stream)
            if message is None:
                break
            try:
                values = eccodes.codes_get_array(message, "values")
                messages.append(
                    {
                        "short_name": eccodes.codes_get(message, "shortName"),
                        "name": eccodes.codes_get(message, "name"),
                        "units": eccodes.codes_get(message, "units"),
                        "validity_date": eccodes.codes_get(message, "validityDate"),
                        "validity_time": eccodes.codes_get(message, "validityTime"),
                        "number_of_points": eccodes.codes_get(message, "numberOfPoints"),
                        "minimum": float(values.min()),
                        "maximum": float(values.max()),
                    }
                )
            finally:
                eccodes.codes_release(message)
    result = {
        "dataset": "reanalysis-era5-land",
        "request": request,
        "raw_file": str(RAW.relative_to(ROOT)).replace("\\", "/"),
        "bytes": RAW.stat().st_size,
        "sha256": digest(RAW),
        "messages": messages,
        "interpretation_limit": "Control de disponibilidad para una hora y fecha. Humedad relativa y velocidad del viento aún no se derivan. No es una serie ni un resumen municipal.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
