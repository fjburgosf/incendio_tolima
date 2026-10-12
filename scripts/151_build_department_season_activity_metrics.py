"""Construye métricas de actividad de fuego por departamento y temporada.

Usa las descargas de `scripts/150_download_regional_fire_activity_series.py` y las
métricas ya declaradas en `configs/regional_fire_activity_series_plan_v1.json`
antes de observar los datos.

La asignación a departamento se hace por contención dentro del polígono
administrativo y no por el rectángulo de cribado. Las celdas de un kilómetro se
definen en una proyección métrica y no por redondeo de coordenadas geográficas.

Una detección no es un incendio, no es área quemada y no es una ignición.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely import Point


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "configs" / "regional_fire_activity_series_plan_v1.json"
RAW_DIR = ROOT / "data" / "raw" / "firms_regional_august_series_2026-09-09"
BOUNDARY = ROOT / "data" / "raw" / "boundaries" / "geoBoundaries-COL-ADM1.geojson"
OUTPUT = ROOT / "data" / "processed" / "regional_fire_activity_department_season_v1.csv"
DETECTIONS_OUT = ROOT / "data" / "interim" / "regional_fire_detections_labelled_v1.csv"
AUDIT = ROOT / "results" / "primary" / "audit_regional_fire_activity_metrics_v1.json"
PROJECTION = "EPSG:3116"
CELL_METRES = 1000.0
HIGH_CONFIDENCE = {"n", "h"}


def normalized(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]+", "", text.upper())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def load_departments(names: list[str]) -> dict[str, object]:
    targets = {normalized(name): name for name in names}
    payload = json.loads(BOUNDARY.read_text(encoding="utf-8"))
    collected: dict[str, list] = defaultdict(list)
    for feature in payload["features"]:
        for value in feature.get("properties", {}).values():
            key = normalized(value)
            if key in targets:
                collected[key].append(shape(feature["geometry"]))
                break
    missing = [name for key, name in targets.items() if key not in collected]
    if missing:
        raise RuntimeError(f"Departamentos ausentes en el límite administrativo, {missing}")
    return {targets[key]: unary_union(geometries) for key, geometries in collected.items()}


def gini(counts: list[int]) -> float:
    if not counts:
        return float("nan")
    values = sorted(counts)
    total = sum(values)
    if total == 0:
        return float("nan")
    n = len(values)
    cumulative = sum((index + 1) * value for index, value in enumerate(values))
    return (2.0 * cumulative) / (n * total) - (n + 1.0) / n


def main() -> None:
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    processing_by_season = {
        int(season): "SP" for season in plan["primary_source"]["seasons"]
    }
    processing_by_season[int(plan["target_season_source"]["season"])] = "NRT"
    department_names = [
        "Tolima", "Caldas", "Cauca", "Cundinamarca", "Huila", "Quindio", "Risaralda", "Valle del Cauca",
    ]
    polygons = load_departments(department_names)
    prepared = {name: prep(geometry) for name, geometry in polygons.items()}
    transformer = Transformer.from_crs("EPSG:4326", PROJECTION, always_xy=True)

    files = sorted(RAW_DIR.glob("*.csv"))
    if not files:
        raise RuntimeError("No hay archivos descargados en el directorio esperado")

    labelled: list[dict] = []
    outside = 0
    out_of_month = 0
    headers = set()
    seen = set()
    duplicates = 0

    for path in files:
        source, _, rest = path.stem.partition("_2")
        season = int("2" + rest.split("-")[0])
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            headers.add(",".join(reader.fieldnames or []))
            for row in reader:
                acquisition = row["acq_date"]
                if not acquisition.startswith(f"{season}-08"):
                    out_of_month += 1
                    continue
                signature = (source, acquisition, row["acq_time"], row["latitude"], row["longitude"])
                if signature in seen:
                    duplicates += 1
                    continue
                seen.add(signature)
                longitude = float(row["longitude"])
                latitude = float(row["latitude"])
                point = Point(longitude, latitude)
                department = None
                for name, geometry in prepared.items():
                    if geometry.contains(point):
                        department = name
                        break
                if department is None:
                    outside += 1
                    continue
                easting, northing = transformer.transform(longitude, latitude)
                labelled.append(
                    {
                        "source": source,
                        "processing_level": "SP" if source.endswith("SP") else "NRT",
                        "season": season,
                        "department": department,
                        "acq_date": acquisition,
                        "acq_time": row["acq_time"],
                        "latitude": latitude,
                        "longitude": longitude,
                        "confidence": row.get("confidence", ""),
                        "fire_type": row.get("type", ""),
                        "frp": row.get("frp", ""),
                        "cell_x": int(math.floor(easting / CELL_METRES)),
                        "cell_y": int(math.floor(northing / CELL_METRES)),
                    }
                )

    grouped: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for record in labelled:
        grouped[(record["department"], record["season"])].append(record)

    seasons = sorted({record["season"] for record in labelled})
    rows = []
    for name in department_names:
        for season in seasons:
            records = grouped.get((name, season), [])
            cells = defaultdict(int)
            for record in records:
                cells[(record["cell_x"], record["cell_y"])] += 1
            confident = [record for record in records if record["confidence"] in HIGH_CONFIDENCE]
            typed = [record for record in records if record["fire_type"] != ""]
            vegetation = [record for record in typed if record["fire_type"] == "0"]
            rows.append(
                {
                    "department": name,
                    "season": season,
                    # El nivel de procesamiento es una propiedad de la fuente y
                    # de la temporada. No puede depender de que el departamento
                    # haya tenido al menos una detección.
                    "processing_level": processing_by_season[season],
                    "detections": len(records),
                    "detections_nominal_or_high_confidence": len(confident),
                    "detections_with_type_field": len(typed),
                    "detections_type_presumed_vegetation": len(vegetation),
                    "active_days": len({record["acq_date"] for record in records}),
                    "cells_1km_with_detection": len(cells),
                    "detections_per_active_cell": round(len(records) / len(cells), 4) if cells else "",
                    "gini_of_cell_counts": round(gini(list(cells.values())), 6) if cells else "",
                }
            )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    DETECTIONS_OUT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    with DETECTIONS_OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(labelled[0].keys()))
        writer.writeheader()
        writer.writerows(labelled)

    audit = {
        "script": Path(__file__).name,
        "executed": "2026-09-09",
        "plan": {"path": str(PLAN_PATH.relative_to(ROOT)), "sha256": sha256(PLAN_PATH)},
        "files_read": len(files),
        "distinct_headers": len(headers),
        "detections_inside_target_departments": len(labelled),
        "detections_outside_target_departments": outside,
        "rows_outside_requested_month": out_of_month,
        "exact_duplicate_rows_discarded": duplicates,
        "header_note": "Los archivos de procesamiento estandar incluyen la columna type y los de tiempo casi real no. Por eso el conteo de encabezados distintos es dos y no uno.",
        "seasons": seasons,
        "departments": department_names,
        "projection_for_cells": PROJECTION,
        "cell_size_metres": CELL_METRES,
        "checks": {
            "single_header_across_files_or_only_type_differs": len(headers) <= 2,
            "all_seasons_present": len(seasons) == 15,
            "every_department_has_rows": all(
                any(row["department"] == name and row["detections"] > 0 for row in rows)
                for name in department_names
            ),
        },
        "interpretation_limits": plan["interpretation_limits"],
        "outputs": [str(OUTPUT.relative_to(ROOT)), str(DETECTIONS_OUT.relative_to(ROOT))],
    }
    audit["gate_passed"] = all(audit["checks"].values())
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    tolima = [row for row in rows if row["department"] == "Tolima"]
    print(json.dumps({"detections": len(labelled), "outside": outside, "duplicates": duplicates,
                      "checks": audit["checks"], "gate_passed": audit["gate_passed"]}, ensure_ascii=False, indent=2))
    print("Tolima por temporada")
    for row in tolima:
        print(f"  {row['season']} {row['processing_level']:>3} detecciones {row['detections']:>6} "
              f"dias {row['active_days']:>3} celdas {row['cells_1km_with_detection']:>5}")


if __name__ == "__main__":
    main()
