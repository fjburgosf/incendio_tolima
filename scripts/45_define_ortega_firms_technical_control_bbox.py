"""Define un rectángulo técnico alrededor de detecciones FIRMS de Ortega, sin delimitar incendio."""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
from statistics import median


ROOT = Path(__file__).resolve().parents[1]
FIRMS_DIRECTORY = ROOT / "data" / "raw" / "firms"
MANIFEST = FIRMS_DIRECTORY / "download_manifest_august_2026.json"
LINKAGE_SCRIPT = ROOT / "scripts" / "06_audit_firms_municipal_linkage.py"
OUTPUT = ROOT / "results" / "exploratory" / "define_ortega_firms_technical_control_bbox.json"
TARGET_DATE = "2026-08-14"
HALF_WIDTH_DEGREES = 0.005


def load_helpers():
    specification = importlib.util.spec_from_file_location("firms_linkage", LINKAGE_SCRIPT)
    if specification is None or specification.loader is None:
        raise RuntimeError("No se pudo cargar el control de enlace FIRMS")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def match(point: tuple[float, float], municipalities: list[dict], contains) -> str | None:
    candidates = []
    for municipality in municipalities:
        west, south, east, north = municipality["bbox"]
        if west <= point[0] <= east and south <= point[1] <= north and contains(point, municipality["geometry"]):
            candidates.append(municipality["name"])
    return candidates[0] if len(candidates) == 1 else None


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("El rectángulo técnico de Ortega ya existe.")
    helpers = load_helpers()
    municipalities = helpers.build_municipal_index()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    points = []
    for entry in manifest["entries"]:
        if entry["source"] != "VIIRS_NOAA20_NRT":
            continue
        with (FIRMS_DIRECTORY / entry["file"]).open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if row["acq_date"] != TARGET_DATE:
                    continue
                point = (float(row["longitude"]), float(row["latitude"]))
                if match(point, municipalities, helpers.geometry_contains) == "ORTEGA":
                    points.append(point)
    if not points:
        raise ValueError("No se encontraron detecciones NOAA 20 asignadas a Ortega en la fecha técnica")
    longitude = median(point[0] for point in points)
    latitude = median(point[1] for point in points)
    result = {
        "purpose": "Control técnico local de clasificación de escena. No es un perímetro ni cicatriz.",
        "selection_rule": "Mediana de coordenadas de detecciones VIIRS NOAA 20 NRT asignadas de forma única a Ortega el 14 de agosto de 2026",
        "detection_count": len(points),
        "center": {"longitude": longitude, "latitude": latitude},
        "bbox_wgs84": [longitude - HALF_WIDTH_DEGREES, latitude - HALF_WIDTH_DEGREES, longitude + HALF_WIDTH_DEGREES, latitude + HALF_WIDTH_DEGREES],
        "interpretation_limit": "La ubicación se deriva de detecciones térmicas NRT sin campo Type. No identifica fuego de vegetación, área quemada ni límites de un incendio.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
