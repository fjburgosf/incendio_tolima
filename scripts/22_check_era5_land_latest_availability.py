"""Comprueba la disponibilidad de una fecha reciente de ERA5 Land.

Descarga una muestra mínima de temperatura a 2 m para el 26 de agosto de 2026.
No genera una serie temporal ni infiere disponibilidad de fechas posteriores.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cdsapi


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
RAW_DIR = ROOT / "data" / "raw" / "era5_land"
OUTPUT = RAW_DIR / "era5_land_availability_2m_temperature_2026-08-26T00.grib"
MANIFEST = RAW_DIR / "era5_land_availability_2m_temperature_2026-08-26T00_manifest.json"
REQUEST = {
    "variable": ["2m_temperature"],
    "year": ["2026"],
    "month": ["08"],
    "day": ["26"],
    "time": ["00:00"],
    "data_format": "grib",
    "download_format": "unarchived",
    "area": [6.2, -76.2, 2.0, -74.0],
}


def read_env(path: Path) -> dict[str, str]:
    values = {}
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


def main() -> None:
    if OUTPUT.exists() or MANIFEST.exists():
        raise FileExistsError(f"La comprobación ya existe en {RAW_DIR}")
    values = read_env(ENV_FILE)
    client = cdsapi.Client(
        url=values["CDS_API_URL"],
        key=values["CDS_API_KEY"],
        quiet=True,
        progress=False,
    )
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    client.retrieve("reanalysis-era5-land", REQUEST).download(str(OUTPUT))
    manifest = {
        "dataset": "reanalysis-era5-land",
        "request": REQUEST,
        "file": OUTPUT.name,
        "bytes": OUTPUT.stat().st_size,
        "sha256": sha256(OUTPUT),
        "purpose": "Comprobación de disponibilidad reciente. No es una serie analítica.",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
