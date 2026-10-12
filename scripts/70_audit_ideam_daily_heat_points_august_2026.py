"""Audit IDEAM daily heat-point CSV files for August 2026 and link to Tolima."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "ideam_heat_points_daily"
BOUNDARY_SOURCE = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
INTERIM = ROOT / "data" / "interim" / "ideam_daily_heat_points_tolima_august_2026.csv"
OUTPUT = ROOT / "results" / "exploratory" / "audit_ideam_daily_heat_points_august_2026.json"
TOLERANCE = 1e-10
EXPECTED_COLUMNS = [
    "Fecha (UTC-5)",
    "Lat",
    "Lon",
    "Fuente",
    "Temperatura (C)",
    "Temperatura Alt* (C)",
    "Radiación térmica (MW)",
    "Confianza",
    "Captura (Dia-Noche)",
    "Scan - real pixel size (km)",
    "Track - real pixel size (km)",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def parse_number(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    return float(value.replace(",", "."))


def on_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> bool:
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    if abs(cross) > TOLERANCE:
        return False
    return (
        min(ax, bx) - TOLERANCE <= px <= max(ax, bx) + TOLERANCE
        and min(ay, by) - TOLERANCE <= py <= max(ay, by) + TOLERANCE
    )


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
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    boundary = False
    for polygon in polygons:
        inside, on_boundary = point_in_polygon(px, py, polygon)
        boundary = boundary or on_boundary
        if inside:
            return True, boundary
    return False, boundary


def geometry_bbox(geometry: dict) -> tuple[float, float, float, float]:
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    coordinates = [coordinate for polygon in polygons for ring in polygon for coordinate in ring]
    return (
        min(item[0] for item in coordinates),
        min(item[1] for item in coordinates),
        max(item[0] for item in coordinates),
        max(item[1] for item in coordinates),
    )


def main() -> None:
    if OUTPUT.exists() or INTERIM.exists():
        raise FileExistsError("El resultado de auditoría ya existe y no se sobrescribirá")
    files = sorted(RAW_DIR.glob("Puntos_de_calor_Colombia_2026-08-*.csv"))
    if len(files) != 31:
        raise ValueError(f"Se esperaban 31 archivos y se encontraron {len(files)}")

    boundaries = json.loads(BOUNDARY_SOURCE.read_text(encoding="utf-8"))["features"]
    indexed_boundaries = [
        (feature["properties"]["mun_nombre"], feature["geometry"], geometry_bbox(feature["geometry"]))
        for feature in boundaries
    ]

    file_manifest = []
    national_rows = 0
    invalid_dates = 0
    invalid_coordinates = 0
    unexpected_file_dates = 0
    duplicate_exact_rows = 0
    seen_rows: set[tuple[str, ...]] = set()
    columns_seen: set[tuple[str, ...]] = set()
    national_source_counts: Counter[str] = Counter()
    missing_counts: Counter[str] = Counter()
    tolima_rows: list[dict[str, object]] = []
    tolima_boundary_matches = 0
    tolima_multiple_matches = 0

    for path in files:
        expected_date = path.stem[-10:]
        row_count = 0
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=";")
            columns = tuple(reader.fieldnames or [])
            columns_seen.add(columns)
            if list(columns) != EXPECTED_COLUMNS:
                raise ValueError(f"Esquema inesperado en {path.name}: {columns}")
            for row in reader:
                row_count += 1
                national_rows += 1
                signature = tuple(row[column] for column in EXPECTED_COLUMNS)
                if signature in seen_rows:
                    duplicate_exact_rows += 1
                seen_rows.add(signature)
                for column in EXPECTED_COLUMNS:
                    if not row[column].strip():
                        missing_counts[column] += 1

                source = row["Fuente"].strip()
                national_source_counts[source] += 1
                try:
                    observed_datetime = datetime.strptime(row["Fecha (UTC-5)"].strip(), "%Y-%m-%d %H:%M")
                    if observed_datetime.date().isoformat() != expected_date:
                        unexpected_file_dates += 1
                except ValueError:
                    invalid_dates += 1
                    continue
                try:
                    latitude = parse_number(row["Lat"])
                    longitude = parse_number(row["Lon"])
                    if latitude is None or longitude is None:
                        raise ValueError("Missing coordinates")
                except ValueError:
                    invalid_coordinates += 1
                    continue

                matches: list[str] = []
                on_boundary = False
                for municipality, geometry, bbox in indexed_boundaries:
                    west, south, east, north = bbox
                    if not (west <= longitude <= east and south <= latitude <= north):
                        continue
                    inside, boundary = point_in_geometry(longitude, latitude, geometry)
                    if inside:
                        matches.append(municipality)
                        on_boundary = on_boundary or boundary
                if len(matches) > 1:
                    tolima_multiple_matches += 1
                if matches:
                    tolima_boundary_matches += int(on_boundary)
                    tolima_rows.append(
                        {
                            "source_file": path.name,
                            "datetime_local_utc_minus_5": observed_datetime.strftime("%Y-%m-%d %H:%M"),
                            "date_local": observed_datetime.date().isoformat(),
                            "time_local": observed_datetime.time().strftime("%H:%M"),
                            "latitude": latitude,
                            "longitude": longitude,
                            "source": source,
                            "temperature_c": parse_number(row["Temperatura (C)"]),
                            "alternate_temperature_c": parse_number(row["Temperatura Alt* (C)"]),
                            "thermal_radiation_mw": parse_number(row["Radiación térmica (MW)"]),
                            "confidence": row["Confianza"].strip(),
                            "day_night": row["Captura (Dia-Noche)"].strip(),
                            "scan_km": parse_number(row["Scan - real pixel size (km)"]),
                            "track_km": parse_number(row["Track - real pixel size (km)"]),
                            "municipality_match_count": len(matches),
                            "municipality": matches[0] if len(matches) == 1 else "|".join(sorted(matches)),
                            "on_polygon_boundary": on_boundary,
                        }
                    )
        file_manifest.append(
            {
                "file": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
                "row_count": row_count,
            }
        )

    INTERIM.parent.mkdir(parents=True, exist_ok=True)
    with INTERIM.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(tolima_rows[0]))
        writer.writeheader()
        writer.writerows(tolima_rows)

    tolima_signatures = [
        (
            row["datetime_local_utc_minus_5"],
            row["latitude"],
            row["longitude"],
            row["source"],
            row["thermal_radiation_mw"],
        )
        for row in tolima_rows
    ]
    result = {
        "official_index": "https://puntosdecalor.ideam.gov.co/archivos-csv/",
        "raw_directory": str(RAW_DIR.relative_to(ROOT)).replace("\\", "/"),
        "boundary_source": str(BOUNDARY_SOURCE.relative_to(ROOT)).replace("\\", "/"),
        "interim_tolima_output": str(INTERIM.relative_to(ROOT)).replace("\\", "/"),
        "file_count": len(files),
        "file_manifest": file_manifest,
        "unique_schemas": [list(item) for item in sorted(columns_seen)],
        "national_row_count": national_rows,
        "national_exact_duplicate_row_count": duplicate_exact_rows,
        "invalid_date_row_count": invalid_dates,
        "invalid_coordinate_row_count": invalid_coordinates,
        "rows_whose_local_date_differs_from_filename": unexpected_file_dates,
        "national_source_counts": dict(sorted(national_source_counts.items())),
        "missing_field_counts": dict(sorted(missing_counts.items())),
        "tolima_row_count": len(tolima_rows),
        "tolima_unique_detection_signatures": len(set(tolima_signatures)),
        "tolima_duplicate_detection_signature_rows": len(tolima_signatures) - len(set(tolima_signatures)),
        "tolima_single_municipality_matches": sum(row["municipality_match_count"] == 1 for row in tolima_rows),
        "tolima_multiple_municipality_matches": tolima_multiple_matches,
        "tolima_polygon_boundary_matches": tolima_boundary_matches,
        "tolima_source_counts": dict(sorted(Counter(str(row["source"]) for row in tolima_rows).items())),
        "tolima_date_counts": dict(sorted(Counter(str(row["date_local"]) for row in tolima_rows).items())),
        "tolima_municipality_counts": dict(
            sorted(Counter(str(row["municipality"]) for row in tolima_rows).items())
        ),
        "tolima_days_with_at_least_one_detection": len(set(row["date_local"] for row in tolima_rows)),
        "interpretation_limit": (
            "IDEAM states that these are NASA FIRMS heat detections clipped to Colombia. They are "
            "not an independent fire-outcome source, burned area, fire count, perimeter, severity, "
            "or exhaustive absence indicator. Sensors remain separate and no confidence filter or "
            "cross-sensor deduplication was applied. Dates are local Colombia time as documented by IDEAM."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "file_manifest"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
