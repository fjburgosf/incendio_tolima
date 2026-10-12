"""Comprueba por separado la disponibilidad de ERA5 Land del 27 al 31 de agosto de 2026."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cdsapi


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
RAW_DIR = ROOT / "data" / "raw" / "era5_land"
OUTPUT = ROOT / "results" / "exploratory" / "check_era5_land_august_tail_availability_2026_09_01.json"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def request_for(day: str) -> dict[str, object]:
    return {
        "variable": ["2m_temperature"],
        "year": ["2026"],
        "month": ["08"],
        "day": [day],
        "time": ["00:00"],
        "data_format": "grib",
        "download_format": "unarchived",
        "area": [6.2, -76.2, 2.0, -74.0],
    }


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("La comprobación de disponibilidad ya existe.")
    values = read_env(ENV_FILE)
    client = cdsapi.Client(url=values["CDS_API_URL"], key=values["CDS_API_KEY"], quiet=True, progress=False)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    checks = []
    for day in ("27", "28", "29", "30", "31"):
        request = request_for(day)
        target = RAW_DIR / f"era5_land_availability_2m_temperature_2026-08-{day}T00.grib"
        try:
            if target.exists():
                raise FileExistsError("El archivo ya existe y no se sobrescribirá")
            client.retrieve("reanalysis-era5-land", request).download(str(target))
            checks.append({"date": f"2026-08-{day}", "available": True, "file": target.name, "bytes": target.stat().st_size, "sha256": sha256(target)})
        except Exception as error:
            checks.append({"date": f"2026-08-{day}", "available": False, "error_type": type(error).__name__, "error_summary": str(error)[:240]})
    result = {"dataset": "reanalysis-era5-land", "checks": checks, "interpretation_limit": "Cada resultado se refiere a una solicitud puntual de temperatura a 2 m. No construye una serie, no valida otros horarios ni otras variables."}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
