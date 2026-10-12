"""Solicita por separado las cuatro horas faltantes del 31 de agosto."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cdsapi
import eccodes


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "era5_land" / "era5_land_core_2026-08-31T20-23Z_supplement_check.grib"
OUTPUT = ROOT / "results" / "exploratory" / "check_era5_land_missing_hours_2026_08_31.json"
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
        raise FileExistsError("El control ya existe y no se sobrescribirá")
    credentials = read_env(ROOT / ".env")
    client = cdsapi.Client(url=credentials["CDS_API_URL"], key=credentials["CDS_API_KEY"], quiet=True, progress=False)
    request = {
        "variable": VARIABLES,
        "year": ["2026"],
        "month": ["08"],
        "day": ["31"],
        "time": ["20:00", "21:00", "22:00", "23:00"],
        "data_format": "grib",
        "download_format": "unarchived",
        "area": [6.2, -76.2, 2.0, -74.0],
    }
    result: dict[str, object] = {"request": request}
    try:
        client.retrieve("reanalysis-era5-land", request).download(str(RAW))
        messages = []
        with RAW.open("rb") as stream:
            while True:
                message = eccodes.codes_grib_new_from_file(stream)
                if message is None:
                    break
                try:
                    messages.append({"short_name": eccodes.codes_get(message, "shortName"), "validity_date": eccodes.codes_get(message, "validityDate"), "validity_time": eccodes.codes_get(message, "validityTime")})
                finally:
                    eccodes.codes_release(message)
        hasher = hashlib.sha256(RAW.read_bytes()).hexdigest().upper()
        result.update({"downloaded": True, "raw_file": str(RAW.relative_to(ROOT)).replace("\\", "/"), "bytes": RAW.stat().st_size, "sha256": hasher, "message_count": len(messages), "messages": messages})
    except Exception as error:
        result.update({"downloaded": False, "error_type": type(error).__name__, "error_summary": str(error)[:500]})
    result["interpretation_limit"] = "Control puntual de disponibilidad. No completa ni modifica los cinco archivos mensuales crudos."
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
