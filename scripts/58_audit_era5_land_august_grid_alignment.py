"""Audita la geometría y alineación de los GRIB ERA5 Land de agosto."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import eccodes
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "era5_land"
OUTPUT = ROOT / "results" / "exploratory" / "audit_era5_land_august_grid_alignment.json"
FILES = sorted(RAW_DIR.glob("era5_land_*_hourly_2026-08_tolima_screening_bbox.grib"))


def coordinate_signature(latitudes: np.ndarray, longitudes: np.ndarray) -> str:
    payload = np.column_stack((latitudes, longitudes)).astype("<f8", copy=False).tobytes()
    return hashlib.sha256(payload).hexdigest().upper()


def first_message_grid(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        message = eccodes.codes_grib_new_from_file(stream)
    if message is None:
        raise ValueError(f"No hay mensaje GRIB en {path.name}")
    try:
        latitudes = eccodes.codes_get_array(message, "latitudes")
        longitudes = eccodes.codes_get_array(message, "longitudes")
        values = eccodes.codes_get_array(message, "values")
        unique_latitudes = np.unique(latitudes)
        unique_longitudes = np.unique(longitudes)
        return {
            "file": str(path.relative_to(ROOT)).replace("\\", "/"),
            "short_name": eccodes.codes_get(message, "shortName"),
            "point_count": int(values.size),
            "latitude_min": float(latitudes.min()),
            "latitude_max": float(latitudes.max()),
            "longitude_min": float(longitudes.min()),
            "longitude_max": float(longitudes.max()),
            "unique_latitude_count": int(unique_latitudes.size),
            "unique_longitude_count": int(unique_longitudes.size),
            "latitude_values_descending": [float(value) for value in unique_latitudes[::-1]],
            "longitude_values_ascending": [float(value) for value in unique_longitudes],
            "coordinate_signature_sha256": coordinate_signature(latitudes, longitudes),
            "missing_value_count_first_message": int(np.isnan(values).sum()),
        }
    finally:
        eccodes.codes_release(message)


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribirá")
    records = [first_message_grid(path) for path in FILES]
    signatures = {record["coordinate_signature_sha256"] for record in records}
    result = {
        "files": records,
        "all_first_message_grids_identical": len(signatures) == 1,
        "unique_grid_signature_count": len(signatures),
        "interpretation_limit": "Verifica la cuadrícula del primer mensaje de cada archivo mensual. No demuestra representatividad física, enlace municipal, exactitud local o ausencia de faltantes en todos los mensajes.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
