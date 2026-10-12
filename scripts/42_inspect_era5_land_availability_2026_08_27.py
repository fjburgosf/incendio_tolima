"""Inspecciona la fecha de validez de la muestra ERA5 Land del 27 de agosto."""

from __future__ import annotations

import json
from pathlib import Path

import eccodes


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "raw" / "era5_land" / "era5_land_availability_2m_temperature_2026-08-27T00.grib"
OUTPUT = ROOT / "results" / "exploratory" / "inspect_era5_land_availability_2026_08_27.json"


def get_value(message, key: str):
    try:
        return eccodes.codes_get(message, key)
    except eccodes.KeyValueNotFoundError:
        return None


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("La inspección ERA5 Land del 27 de agosto ya existe.")
    with SOURCE.open("rb") as stream:
        message = eccodes.codes_grib_new_from_file(stream)
    if message is None:
        raise ValueError("No se encontró mensaje GRIB")
    try:
        values = eccodes.codes_get_array(message, "values")
        audit = {
            "source_file": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
            "edition": get_value(message, "edition"),
            "short_name": get_value(message, "shortName"),
            "parameter_name": get_value(message, "name"),
            "units": get_value(message, "units"),
            "date": get_value(message, "dataDate"),
            "time": get_value(message, "dataTime"),
            "validity_date": get_value(message, "validityDate"),
            "validity_time": get_value(message, "validityTime"),
            "step_range": get_value(message, "stepRange"),
            "number_of_points": get_value(message, "numberOfPoints"),
            "minimum_value": float(values.min()),
            "maximum_value": float(values.max()),
            "interpretation_limit": "La descarga prueba disponibilidad puntual de esta fecha y hora de validez. No construye una serie ni comprueba otras variables.",
        }
    finally:
        eccodes.codes_release(message)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
