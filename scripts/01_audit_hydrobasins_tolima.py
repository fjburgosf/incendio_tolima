"""Audita la disponibilidad espacial de HydroBASINS nivel 12 para Tolima.

No modifica datos crudos. La prueba usa solapamiento de cajas envolventes y
no sustituye una intersección geométrica final.
"""
from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOUNDARY = ROOT / "data" / "raw" / "boundaries" / "geoBoundaries-COL-ADM1.geojson"
HYBAS = ROOT / "data" / "raw" / "hydrobasins" / "hybas_sa_lev01-12_v1c.zip"
OUTPUT = ROOT / "results" / "exploratory" / "audit_hydrobasins_tolima.json"


def flatten_coordinates(value: object) -> list[tuple[float, float]]:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(
        isinstance(v, (int, float)) for v in value
    ):
        return [(float(value[0]), float(value[1]))]
    if isinstance(value, (list, tuple)):
        points: list[tuple[float, float]] = []
        for part in value:
            points.extend(flatten_coordinates(part))
        return points
    raise ValueError("Coordenadas GeoJSON inválidas")


def tolima_bbox() -> tuple[float, float, float, float]:
    data = json.loads(BOUNDARY.read_text(encoding="utf-8"))
    matches = [
        feature
        for feature in data["features"]
        if feature.get("properties", {}).get("shapeISO") == "CO-TOL"
    ]
    if len(matches) != 1:
        raise ValueError("No se identificó una única entidad CO-TOL")
    points = flatten_coordinates(matches[0]["geometry"]["coordinates"])
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def hydrobasins_bbox_count(query: tuple[float, float, float, float]) -> int:
    with zipfile.ZipFile(HYBAS) as archive:
        with archive.open("hybas_sa_lev12_v1c.shp") as source:
            header = source.read(100)
            file_length_words = struct.unpack(">i", header[24:28])[0]
            expected_bytes = file_length_words * 2
            source.seek(0, 2)
            actual_bytes = source.tell()
            if actual_bytes != expected_bytes:
                raise ValueError("Longitud del shapefile inconsistente")
            source.seek(100)
            count = 0
            while source.tell() < actual_bytes:
                record_header = source.read(8)
                if len(record_header) != 8:
                    raise ValueError("Cabecera de registro incompleta")
                content_words = struct.unpack(">i", record_header[4:8])[0]
                content = source.read(content_words * 2)
                if len(content) != content_words * 2:
                    raise ValueError("Registro de geometría incompleto")
                shape_type = struct.unpack("<i", content[:4])[0]
                if shape_type == 0:
                    continue
                if shape_type not in {5, 15, 25}:
                    raise ValueError(f"Tipo de geometría inesperado {shape_type}")
                box = struct.unpack("<4d", content[4:36])
                if overlaps(box, query):
                    count += 1
    return count


def main() -> None:
    bbox = tolima_bbox()
    count = hydrobasins_bbox_count(bbox)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "method": "Solapamiento de cajas envolventes entre CO TOL y HydroBASINS nivel 12",
        "limitation": "La prueba no es una intersección geométrica final",
        "tolima_bbox_lonlat": [round(value, 6) for value in bbox],
        "candidate_subbasins_bbox_overlap": count,
        "hydrobasins_file": str(HYBAS.relative_to(ROOT)),
        "boundary_file": str(BOUNDARY.relative_to(ROOT)),
    }
    OUTPUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
