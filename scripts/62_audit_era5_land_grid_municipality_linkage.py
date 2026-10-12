"""Audita enlace entre centros de la cuadrícula ERA5 Land y municipios Cortolima."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from pathlib import Path

import eccodes


ROOT = Path(__file__).resolve().parents[1]
GRID_SOURCE = ROOT / "data" / "raw" / "era5_land" / "era5_land_2t_hourly_2026-08_tolima_screening_bbox.grib"
BOUNDARY_SOURCE = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
OUTPUT = ROOT / "results" / "exploratory" / "audit_era5_land_grid_municipality_linkage.json"
INTERIM = ROOT / "data" / "interim" / "era5_land_grid_municipality_linkage.csv"
FOCUS = {"ORTEGA", "COYAIMA", "GUAMO", "SAN LUIS", "CHAPARRAL"}
TOLERANCE = 1e-10


def on_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> bool:
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    if abs(cross) > TOLERANCE:
        return False
    return min(ax, bx) - TOLERANCE <= px <= max(ax, bx) + TOLERANCE and min(ay, by) - TOLERANCE <= py <= max(ay, by) + TOLERANCE


def point_in_ring(px: float, py: float, ring: list[list[float]]) -> tuple[bool, bool]:
    inside = False
    for index in range(len(ring) - 1):
        ax, ay = ring[index][:2]
        bx, by = ring[index + 1][:2]
        if on_segment(px, py, ax, ay, bx, by):
            return True, True
        if (ay > py) != (by > py):
            crossing_x = (bx - ax) * (py - ay) / (by - ay) + ax
            if px < crossing_x:
                inside = not inside
    return inside, False


def point_in_polygon(px: float, py: float, rings: list[list[list[float]]]) -> tuple[bool, bool]:
    outer, boundary = point_in_ring(px, py, rings[0])
    if boundary:
        return True, True
    if not outer:
        return False, False
    for hole in rings[1:]:
        in_hole, hole_boundary = point_in_ring(px, py, hole)
        if hole_boundary:
            return True, True
        if in_hole:
            return False, False
    return True, False


def point_in_geometry(px: float, py: float, geometry: dict) -> tuple[bool, bool]:
    if geometry["type"] == "Polygon":
        return point_in_polygon(px, py, geometry["coordinates"])
    if geometry["type"] == "MultiPolygon":
        boundary = False
        for polygon in geometry["coordinates"]:
            inside, on_boundary = point_in_polygon(px, py, polygon)
            boundary = boundary or on_boundary
            if inside:
                return True, boundary
        return False, boundary
    raise ValueError(f"Geometría no soportada: {geometry['type']}")


def geometry_bbox(geometry: dict) -> tuple[float, float, float, float]:
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    xs = []
    ys = []
    for polygon in polygons:
        for ring in polygon:
            for coordinate in ring:
                xs.append(coordinate[0])
                ys.append(coordinate[1])
    return min(xs), min(ys), max(xs), max(ys)


def read_grid() -> list[tuple[float, float]]:
    with GRID_SOURCE.open("rb") as stream:
        message = eccodes.codes_grib_new_from_file(stream)
    if message is None:
        raise ValueError("No hay mensaje GRIB")
    try:
        latitudes = eccodes.codes_get_array(message, "latitudes")
        longitudes = eccodes.codes_get_array(message, "longitudes")
        return sorted({(round(float(lon), 10), round(float(lat), 10)) for lon, lat in zip(longitudes, latitudes)})
    finally:
        eccodes.codes_release(message)


def main() -> None:
    if OUTPUT.exists() or INTERIM.exists():
        raise FileExistsError("El resultado de enlace ya existe y no se sobrescribirá")
    boundaries = json.loads(BOUNDARY_SOURCE.read_text(encoding="utf-8"))
    features = [
        (feature, geometry_bbox(feature["geometry"]))
        for feature in boundaries["features"]
    ]
    rows = []
    boundary_count = 0
    for grid_id, (longitude, latitude) in enumerate(read_grid(), start=1):
        matches = []
        boundary_match = False
        for feature, bbox in features:
            west, south, east, north = bbox
            if not (west - TOLERANCE <= longitude <= east + TOLERANCE and south - TOLERANCE <= latitude <= north + TOLERANCE):
                continue
            inside, on_boundary = point_in_geometry(longitude, latitude, feature["geometry"])
            if inside:
                matches.append(feature["properties"]["mun_nombre"])
                boundary_match = boundary_match or on_boundary
        boundary_count += int(boundary_match)
        rows.append(
            {
                "grid_id": grid_id,
                "longitude": longitude,
                "latitude": latitude,
                "municipality_match_count": len(matches),
                "municipality": matches[0] if len(matches) == 1 else "|".join(sorted(matches)),
                "on_polygon_boundary": boundary_match,
            }
        )
    INTERIM.parent.mkdir(parents=True, exist_ok=True)
    with INTERIM.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    counts = Counter(row["municipality"] for row in rows if row["municipality_match_count"] == 1)
    result = {
        "grid_source": str(GRID_SOURCE.relative_to(ROOT)).replace("\\", "/"),
        "boundary_source": str(BOUNDARY_SOURCE.relative_to(ROOT)).replace("\\", "/"),
        "interim_linkage": str(INTERIM.relative_to(ROOT)).replace("\\", "/"),
        "grid_point_count": len(rows),
        "unique_single_municipality_matches": sum(row["municipality_match_count"] == 1 for row in rows),
        "no_municipality_match": sum(row["municipality_match_count"] == 0 for row in rows),
        "multiple_municipality_matches": sum(row["municipality_match_count"] > 1 for row in rows),
        "polygon_boundary_matches": boundary_count,
        "focus_municipality_grid_center_counts": {name: counts.get(name, 0) for name in sorted(FOCUS)},
        "all_municipality_grid_center_counts": dict(sorted(counts.items())),
        "interpretation_limit": "Enlace por centro de celda a polígono municipal. Una celda cuyo centro está fuera puede intersectar el municipio. No es una interpolación, promedio municipal, estación ni prueba de representatividad local.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
