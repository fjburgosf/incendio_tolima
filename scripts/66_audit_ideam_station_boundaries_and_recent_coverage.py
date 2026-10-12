"""Audit IDEAM station coordinates against Cortolima polygons and recent coverage."""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections import Counter
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATION_LINKAGE = ROOT / "data" / "interim" / "ideam_precipitation_station_catalog_linkage.csv"
BOUNDARIES = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
RAW_PRECIP_DIR = ROOT / "data" / "raw" / "ideam"
OUTPUT_CSV = ROOT / "data" / "interim" / "ideam_precipitation_station_recent_coverage.csv"
OUTPUT_JSON = ROOT / "results" / "exploratory" / "audit_ideam_station_boundaries_and_recent_coverage.json"
TOLERANCE = 1e-10

PERIODS = {
    "june_2026": (date(2026, 6, 1), date(2026, 6, 30)),
    "july_2026": (date(2026, 7, 1), date(2026, 7, 31)),
    "august_2026": (date(2026, 8, 1), date(2026, 8, 31)),
    "june_to_august_2026": (date(2026, 6, 1), date(2026, 8, 31)),
}


def normalize_municipality(value: str) -> str:
    value = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()
    value = unicodedata.normalize("NFKD", value)
    return "".join(char for char in value if not unicodedata.combining(char)).upper()


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
    on_any_boundary = False
    for polygon in polygons:
        inside, boundary = point_in_polygon(px, py, polygon)
        on_any_boundary = on_any_boundary or boundary
        if inside:
            return True, on_any_boundary
    return False, on_any_boundary


def load_dates() -> tuple[dict[str, set[date]], dict[str, Counter]]:
    dates: dict[str, set[date]] = {}
    approval: dict[str, Counter] = {}
    for path in sorted(RAW_PRECIP_DIR.glob("Precipitacion*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                code = row["CodigoEstacion"].strip()
                observation_date = datetime.strptime(row["Fecha"], "%Y-%m-%d %H:%M").date()
                dates.setdefault(code, set()).add(observation_date)
                approval.setdefault(code, Counter())[row["NivelAprobacion"].strip()] += 1
    return dates, approval


def main() -> None:
    if OUTPUT_CSV.exists() or OUTPUT_JSON.exists():
        raise FileExistsError("El resultado de auditoría ya existe y no se sobrescribirá")

    with STATION_LINKAGE.open(encoding="utf-8-sig", newline="") as handle:
        stations = list(csv.DictReader(handle))
    polygons = json.loads(BOUNDARIES.read_text(encoding="utf-8"))["features"]
    station_dates, approval_counts = load_dates()

    rows: list[dict[str, object]] = []
    for station in stations:
        code = station["station_code"]
        longitude = float(station["geometry_x_epsg4326"])
        latitude = float(station["geometry_y_epsg4326"])
        matches: list[str] = []
        on_boundary = False
        for feature in polygons:
            inside, boundary = point_in_geometry(longitude, latitude, feature["geometry"])
            if inside:
                matches.append(feature["properties"]["mun_nombre"])
                on_boundary = on_boundary or boundary

        dates = station_dates.get(code, set())
        row: dict[str, object] = {
            "station_code": code,
            "station_name": station["station_name"],
            "official_municipality": station["official_municipality"],
            "polygon_match_count": len(matches),
            "polygon_municipality": "|".join(sorted(matches)),
            "official_municipality_matches_polygon": (
                len(matches) == 1
                and normalize_municipality(station["official_municipality"])
                == normalize_municipality(matches[0])
            ),
            "on_polygon_boundary": on_boundary,
            "all_observed_date_count": len(dates),
            "all_first_date": min(dates).isoformat() if dates else None,
            "all_last_date": max(dates).isoformat() if dates else None,
            "all_approval_levels_and_rows": json.dumps(
                dict(sorted(approval_counts.get(code, Counter()).items())),
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
        for label, (start, end) in PERIODS.items():
            expected = (end - start).days + 1
            observed = sum(start <= item <= end for item in dates)
            row[f"{label}_expected_dates"] = expected
            row[f"{label}_observed_dates"] = observed
            row[f"{label}_coverage_percent"] = round(observed / expected * 100, 1)
        rows.append(row)

    rows.sort(key=lambda item: str(item["station_code"]))
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    report = {
        "station_linkage": str(STATION_LINKAGE.relative_to(ROOT)).replace("\\", "/"),
        "boundary_source": str(BOUNDARIES.relative_to(ROOT)).replace("\\", "/"),
        "raw_precipitation_directory": str(RAW_PRECIP_DIR.relative_to(ROOT)).replace("\\", "/"),
        "interim_output": str(OUTPUT_CSV.relative_to(ROOT)).replace("\\", "/"),
        "station_count": len(rows),
        "single_polygon_matches": sum(row["polygon_match_count"] == 1 for row in rows),
        "no_polygon_matches": sum(row["polygon_match_count"] == 0 for row in rows),
        "multiple_polygon_matches": sum(row["polygon_match_count"] > 1 for row in rows),
        "polygon_boundary_matches": sum(row["on_polygon_boundary"] is True for row in rows),
        "official_municipality_polygon_matches": sum(
            row["official_municipality_matches_polygon"] is True for row in rows
        ),
        "official_municipality_polygon_mismatches": sum(
            row["official_municipality_matches_polygon"] is False for row in rows
        ),
        "complete_station_counts": {
            label: sum(row[f"{label}_observed_dates"] == row[f"{label}_expected_dates"] for row in rows)
            for label in PERIODS
        },
        "interpretation_limit": (
            "Date coverage counts rows present for each station and calendar day. An absent row is "
            "treated as an unavailable observation, not as zero precipitation. Polygon checks use "
            "catalog coordinates and the Cortolima municipal layer and do not establish gauge "
            "representativeness beyond the station location."
        ),
    }
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    for row in rows:
        print(
            row["station_code"],
            row["official_municipality"],
            row["polygon_municipality"],
            row["june_2026_observed_dates"],
            row["july_2026_observed_dates"],
            row["august_2026_observed_dates"],
            sep=" | ",
        )


if __name__ == "__main__":
    main()
