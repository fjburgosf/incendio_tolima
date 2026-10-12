"""Audita el enlace geográfico entre puntos FIRMS y municipios de Cortolima.

No modifica los archivos crudos ni interpreta detecciones como incendios. El
resultado solo evalúa si las coordenadas pueden asignarse espacialmente a una
referencia municipal pública.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
FIRMS_DIRECTORY = ROOT / "data" / "raw" / "firms"
MANIFEST = FIRMS_DIRECTORY / "download_manifest_august_2026.json"
OUTPUT = ROOT / "results" / "exploratory" / "audit_firms_municipal_linkage_august_2026.json"
EPSILON = 1e-12


def point_on_segment(point: tuple[float, float], start: list[float], end: list[float]) -> bool:
    x, y = point
    x1, y1 = start[:2]
    x2, y2 = end[:2]
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > EPSILON:
        return False
    return min(x1, x2) - EPSILON <= x <= max(x1, x2) + EPSILON and min(y1, y2) - EPSILON <= y <= max(y1, y2) + EPSILON


def point_in_ring(point: tuple[float, float], ring: list[list[float]]) -> bool:
    x, y = point
    inside = False
    for start, end in zip(ring, ring[1:] + ring[:1]):
        if point_on_segment(point, start, end):
            return True
        x1, y1 = start[:2]
        x2, y2 = end[:2]
        crosses = (y1 > y) != (y2 > y)
        if crosses:
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection_x:
                inside = not inside
    return inside


def point_in_polygon(point: tuple[float, float], polygon: list[list[list[float]]]) -> bool:
    if not polygon or not point_in_ring(point, polygon[0]):
        return False
    return not any(point_in_ring(point, hole) for hole in polygon[1:])


def coordinate_pairs(geometry: dict) -> list[list[float]]:
    if geometry["type"] == "Polygon":
        return [coordinate for ring in geometry["coordinates"] for coordinate in ring]
    return [
        coordinate
        for polygon in geometry["coordinates"]
        for ring in polygon
        for coordinate in ring
    ]


def geometry_contains(point: tuple[float, float], geometry: dict) -> bool:
    if geometry["type"] == "Polygon":
        return point_in_polygon(point, geometry["coordinates"])
    if geometry["type"] == "MultiPolygon":
        return any(point_in_polygon(point, polygon) for polygon in geometry["coordinates"])
    raise ValueError(f"Geometría no admitida {geometry['type']}")


def build_municipal_index() -> list[dict]:
    collection = json.loads(BOUNDARIES.read_text(encoding="utf-8"))
    indexed = []
    for feature in collection["features"]:
        geometry = feature["geometry"]
        pairs = coordinate_pairs(geometry)
        xs = [pair[0] for pair in pairs]
        ys = [pair[1] for pair in pairs]
        indexed.append(
            {
                "name": feature["properties"]["mun_nombre"],
                "geometry": geometry,
                "bbox": (min(xs), min(ys), max(xs), max(ys)),
            }
        )
    return indexed


def main() -> None:
    municipalities = build_municipal_index()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    status_by_source = defaultdict(Counter)
    total = Counter()
    municipalities_with_matches: set[str] = set()
    municipality_days_with_matches: set[tuple[str, str]] = set()
    calendar_days_with_matches: set[str] = set()

    for entry in manifest["entries"]:
        source = entry["source"]
        with (FIRMS_DIRECTORY / entry["file"]).open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                point = (float(row["longitude"]), float(row["latitude"]))
                candidates = [
                    municipality
                    for municipality in municipalities
                    if municipality["bbox"][0] <= point[0] <= municipality["bbox"][2]
                    and municipality["bbox"][1] <= point[1] <= municipality["bbox"][3]
                ]
                matches = [
                    municipality["name"]
                    for municipality in candidates
                    if geometry_contains(point, municipality["geometry"])
                ]
                if len(matches) == 1:
                    status = "single_municipal_match"
                    municipalities_with_matches.add(matches[0])
                    municipality_days_with_matches.add((matches[0], row["acq_date"]))
                    calendar_days_with_matches.add(row["acq_date"])
                elif not matches:
                    status = "no_municipal_match"
                else:
                    status = "multiple_municipal_matches"
                status_by_source[source][status] += 1
                total[status] += 1

    audit = {
        "linkage_type": "geographic",
        "point_source": "FIRMS VIIRS NRT, agosto de 2026",
        "polygon_source": "LímiteMunicipal 25K de Cortolima",
        "boundary_rule": "Los puntos sobre borde se consideran dentro del polígono",
        "source_counts": {source: dict(sorted(counts.items())) for source, counts in sorted(status_by_source.items())},
        "total_counts": dict(sorted(total.items())),
        "municipalities_with_at_least_one_point": len(municipalities_with_matches),
        "candidate_municipality_day_cells_with_at_least_one_detection": len(municipality_days_with_matches),
        "calendar_days_with_at_least_one_matched_detection": len(calendar_days_with_matches),
        "candidate_outcome_limit": (
            "Una celda municipio día con detección puede describirse como detección térmica FIRMS. "
            "Una celda sin detección no puede tratarse como ausencia de incendio, porque este control "
            "no demuestra observación completa ni detectabilidad perfecta."
        ),
        "interpretation_limit": (
            "Este control prueba factibilidad técnica de un enlace geográfico. No atribuye una "
            "detección a un incendio de vegetación, no estima área quemada y no valida límites municipales."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
