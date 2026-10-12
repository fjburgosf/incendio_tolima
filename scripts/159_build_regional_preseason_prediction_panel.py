"""Build the audited department by season panel for later prediction models.

The script joins the pre-defined August activity outcome to antecedent CHIRPS
features and lagged MapBiomas coverage. It deliberately does not transform,
standardise, select, fit or evaluate predictors. Those operations must happen
inside temporal validation folds in a later script to avoid leakage.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path

from pyproj import Geod
from shapely.geometry import shape


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "configs" / "regional_preseason_prediction_plan_v1.json"
ACTIVITY_PATH = ROOT / "data" / "processed" / "regional_fire_activity_department_season_v1.csv"
CHIRPS_PATH = ROOT / "data" / "processed" / "chirps_regional_antecedent_precipitation_v1.csv"
MAPBIOMAS_PATH = ROOT / "data" / "interim" / "mapbiomas_regional_landcover_panel_v1.csv"
BOUNDARY_PATH = ROOT / "data" / "raw" / "boundaries" / "geoBoundaries-COL-ADM1.geojson"
OUTPUT_PATH = ROOT / "data" / "processed" / "regional_preseason_prediction_panel_v1.csv"
AUDIT_PATH = ROOT / "results" / "primary" / "audit_regional_preseason_prediction_panel_v1.json"
GEOD = Geod(ellps="WGS84")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def normalized(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]+", "", text.upper())


def geodesic_area_ha(geometry) -> float:
    total = 0.0
    geometries = geometry.geoms if geometry.geom_type == "MultiPolygon" else [geometry]
    for polygon in geometries:
        longitude, latitude = zip(*polygon.exterior.coords)
        area, _ = GEOD.polygon_area_perimeter(longitude, latitude)
        total += abs(area)
        for interior in polygon.interiors:
            longitude, latitude = zip(*interior.coords)
            area, _ = GEOD.polygon_area_perimeter(longitude, latitude)
            total -= abs(area)
    return total / 10000.0


def boundary_areas(departments: list[str]) -> dict[str, float]:
    targets = {normalized(name): name for name in departments}
    results: dict[str, float] = {}
    payload = json.loads(BOUNDARY_PATH.read_text(encoding="utf-8"))
    for feature in payload["features"]:
        hits = {targets[normalized(value)] for value in feature.get("properties", {}).values() if normalized(value) in targets}
        if len(hits) > 1:
            raise RuntimeError("A boundary feature matches multiple target departments")
        if hits:
            name = hits.pop()
            if name in results:
                raise RuntimeError(f"Multiple boundary features match {name}")
            results[name] = geodesic_area_ha(shape(feature["geometry"]))
    if set(results) != set(departments):
        raise RuntimeError(f"Boundary area coverage mismatch: {sorted(set(departments) - set(results))}")
    return results


def coverage_key(class_name: str) -> str | None:
    value = normalized(class_name)
    if value.startswith("1FORMACIONBOSCOSA"):
        return "forest"
    if value.startswith("2FORMACIONNATURALNOBOSCOSA"):
        return "natural_nonforest"
    if value.startswith("3AREAAGROPECUARIA"):
        return "agricultural"
    return None


def main() -> None:
    if OUTPUT_PATH.exists() or AUDIT_PATH.exists():
        raise FileExistsError("Prediction panel output or audit exists and must not be overwritten")
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    departments = plan["geography"]["departments"]
    seasons = plan["temporal_design"]["seasons"]
    activity = {(row["department"], int(row["season"])): row for row in read_csv(ACTIVITY_PATH)}
    chirps = {(row["department"], int(row["season"])): row for row in read_csv(CHIRPS_PATH)}
    if set(activity) != {(department, season) for department in departments for season in seasons}:
        raise RuntimeError("Activity panel does not have exactly one row per frozen department season")
    if set(chirps) != set(activity):
        raise RuntimeError("CHIRPS panel does not match the activity panel")
    mapbiomas_rows = read_csv(MAPBIOMAS_PATH)
    coverage: dict[tuple[str, int], dict[str, float]] = {}
    for row in mapbiomas_rows:
        department = row["department"]
        if department not in departments:
            continue
        key = (department, int(row["year"]))
        bucket = coverage.setdefault(key, {"forest": 0.0, "natural_nonforest": 0.0, "agricultural": 0.0, "classified_area_ha": 0.0})
        fraction = float(row["fraction_of_department_classified"])
        bucket["classified_area_ha"] += float(row["area_ha"])
        mapped = coverage_key(row["class_level_1"])
        if mapped:
            bucket[mapped] += fraction
    expected_coverage_years = {(department, year) for department in departments for year in range(2011, 2025)}
    if not expected_coverage_years <= set(coverage):
        raise RuntimeError("MapBiomas coverage is insufficient for lagged 2012 through 2025 features")
    areas = boundary_areas(departments)
    output_rows = []
    carried_forward = 0
    for department in departments:
        for season in seasons:
            coverage_year = season - 1 if season <= 2025 else 2024
            carry = int(season >= 2025)
            carried_forward += carry
            cover = coverage[(department, coverage_year)]
            a = activity[(department, season)]
            rain = chirps[(department, season)]
            output_rows.append(
                {
                    "department": department,
                    "season": season,
                    "processing_level": a["processing_level"],
                    "department_area_ha": round(areas[department], 6),
                    "log_department_area_ha": round(math.log(areas[department]), 8),
                    "detections": int(a["detections"]),
                    "detections_nominal_or_high_confidence": int(a["detections_nominal_or_high_confidence"]),
                    "cells_1km_with_detection": int(a["cells_1km_with_detection"]),
                    "active_days": int(a["active_days"]),
                    # La concentración es indefinida cuando no hay celdas
                    # activas. Se conserva como ausencia semántica, no como
                    # cero. Esta métrica es descriptiva y no entra al modelo
                    # predictivo preespecificado.
                    "gini_of_cell_counts": (
                        float(a["gini_of_cell_counts"])
                        if a["gini_of_cell_counts"].strip()
                        else ""
                    ),
                    "chirps_july_mean_mm": float(rain["chirps_july_mean_mm"]),
                    "chirps_may_july_mean_mm": float(rain["chirps_may_july_mean_mm"]),
                    "mapbiomas_source_year": coverage_year,
                    "mapbiomas_carried_forward": carry,
                    "mapbiomas_forest_fraction_lagged": round(cover["forest"], 8),
                    "mapbiomas_natural_nonforest_fraction_lagged": round(cover["natural_nonforest"], 8),
                    "mapbiomas_agricultural_fraction_lagged": round(cover["agricultural"], 8),
                    "mapbiomas_classified_area_ha_lagged": round(cover["classified_area_ha"], 6),
                }
            )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0].keys()))
        writer.writeheader()
        writer.writerows(output_rows)
    audit = {
        "script": Path(__file__).name,
        "plan": str(PLAN_PATH.relative_to(ROOT)).replace("\\", "/"),
        "row_count": len(output_rows),
        "expected_row_count": len(departments) * len(seasons),
        "carried_forward_land_cover_rows": carried_forward,
        "land_cover_temporal_rule": "outcome season t receives MapBiomas t minus 1, except 2025 and 2026 receive 2024 with explicit flag",
        "inputs": [
            {"path": str(ACTIVITY_PATH.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(ACTIVITY_PATH)},
            {"path": str(CHIRPS_PATH.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(CHIRPS_PATH)},
            {"path": str(MAPBIOMAS_PATH.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(MAPBIOMAS_PATH)},
            {"path": str(BOUNDARY_PATH.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(BOUNDARY_PATH)},
        ],
        "output": {"path": str(OUTPUT_PATH.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(OUTPUT_PATH)},
        "limits": [
            "The 2026 outcome uses NRT whereas reference outcomes use standard processing.",
            "CHIRPS May through July is a 92 day calendar total, not an exact 90 day rolling accumulation.",
            "MapBiomas coverage is lagged by one year. The 2025 and 2026 rows carry the 2024 map and must be flagged in every model output.",
            "This panel does not include soil moisture, roads, protected areas, UPRA or direct measures of ignition cause.",
        ],
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(output_rows), "carried_forward_land_cover_rows": carried_forward, "output": str(OUTPUT_PATH.relative_to(ROOT))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
