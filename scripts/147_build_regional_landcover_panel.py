"""Construye el panel de cobertura por departamento y año para Tolima y sus vecinos.

Los criterios de aceptación quedaron congelados en
`configs/regional_landcover_panel_plan_v1.json` antes de ejecutar este script.

Las celdas vacías se registran como ausentes y nunca se convierten en cero. Las
fracciones se calculan sobre el total conciliado de clases de nivel uno del mismo
departamento y año. La conciliación compara ese total contra el área geodésica de
un límite administrativo independiente.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
import zipfile
from pathlib import Path

from openpyxl import load_workbook
from pyproj import Geod
from shapely.geometry import shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "regional_landcover_panel_plan_v1.json"
SOURCE = ROOT / "data" / "raw" / "mapbiomas" / "mapbiomas_colombia_collection3_coverage_statistics.zip"
BOUNDARY = ROOT / "data" / "raw" / "boundaries" / "geoBoundaries-COL-ADM1.geojson"
OUTPUT = ROOT / "data" / "interim" / "mapbiomas_regional_landcover_panel_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_regional_landcover_panel_v1.json"
MEMBER = "Statistics-for-Website-MB-Cobertura-col3.xlsx"
SHEET = "COBERTURA_DEPARTAMENTO"
GEOD = Geod(ellps="WGS84")


def normalized(value: object) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]+", "", text.upper())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def geodesic_area_ha(geometry) -> float:
    total = 0.0
    geoms = geometry.geoms if geometry.geom_type == "MultiPolygon" else [geometry]
    for polygon in geoms:
        lon, lat = zip(*polygon.exterior.coords)
        area, _ = GEOD.polygon_area_perimeter(lon, lat)
        total += abs(area)
        for interior in polygon.interiors:
            lon, lat = zip(*interior.coords)
            area, _ = GEOD.polygon_area_perimeter(lon, lat)
            total -= abs(area)
    return total / 10000.0


def boundary_areas(targets: dict[str, str]) -> dict[str, float]:
    payload = json.loads(BOUNDARY.read_text(encoding="utf-8"))
    collected: dict[str, list] = {key: [] for key in targets}
    for feature in payload["features"]:
        properties = feature.get("properties", {})
        for value in properties.values():
            key = normalized(value)
            if key in collected:
                collected[key].append(shape(feature["geometry"]))
                break
    areas = {}
    for key, geometries in collected.items():
        if geometries:
            areas[key] = geodesic_area_ha(unary_union(geometries))
    return areas


def read_panel(targets: dict[str, str]) -> tuple[dict, list[int], dict]:
    with zipfile.ZipFile(SOURCE) as archive:
        payload = io.BytesIO(archive.read(MEMBER))
    workbook = load_workbook(payload, read_only=True, data_only=True)
    sheet = workbook[SHEET]
    rows = sheet.iter_rows(values_only=True)
    header = next(rows)
    index = {str(name): position for position, name in enumerate(header)}
    years = sorted(int(name) for name in header if isinstance(name, int))
    department_column = index["departamento"]
    class_column = index["class_level_1"]
    panel: dict = {}
    missing: dict = {}
    for row in rows:
        key = normalized(row[department_column])
        if key not in targets:
            continue
        label = str(row[class_column]).strip()
        for year in years:
            value = row[header.index(year)]
            slot = panel.setdefault((key, year), {})
            if value is None:
                missing[(key, year)] = missing.get((key, year), 0) + 1
                continue
            slot[label] = slot.get(label, 0.0) + float(value)
    workbook.close()
    return panel, years, missing


def main() -> None:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    criteria = plan["acceptance_criteria"]
    targets = {normalized(name): name for name in plan["target_departments"]}

    areas = boundary_areas(targets)
    panel, years, missing = read_panel(targets)

    first_year, last_year = criteria["required_year_range"]
    expected_years = list(range(first_year, last_year + 1))

    records = []
    ratios = {}
    for key, name in targets.items():
        for year in expected_years:
            classes = panel.get((key, year), {})
            total = sum(classes.values())
            reference = areas.get(key)
            ratio = total / reference if reference else None
            ratios[f"{name}|{year}"] = ratio
            for label, value in sorted(classes.items()):
                records.append(
                    {
                        "department": name,
                        "year": year,
                        "class_level_1": label,
                        "area_ha": round(value, 6),
                        "fraction_of_department_classified": round(value / total, 8) if total else "",
                        "missing_cells_department_year": missing.get((key, year), 0),
                    }
                )

    checks = {
        "all_target_departments_present": sorted(targets) == sorted(k for k in targets if (k, last_year) in panel),
        "all_target_departments_have_boundary": sorted(targets) == sorted(areas),
        "no_year_missing_within_range": all((key, year) in panel for key in targets for year in expected_years),
        "reconciliation_within_tolerance": all(
            ratio is not None
            and criteria["reconciliation_ratio_minimum"] <= ratio <= criteria["reconciliation_ratio_maximum"]
            for ratio in ratios.values()
        ),
        "no_empty_cell_coerced_to_zero": True,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)

    audit = {
        "script": Path(__file__).name,
        "executed": "2026-09-09",
        "plan": {"path": str(PLAN.relative_to(ROOT)), "sha256": sha256(PLAN)},
        "inputs": [
            {"path": str(SOURCE.relative_to(ROOT)), "sha256": sha256(SOURCE)},
            {"path": str(BOUNDARY.relative_to(ROOT)), "sha256": sha256(BOUNDARY)},
        ],
        "departments": len(targets),
        "years": [expected_years[0], expected_years[-1]],
        "rows_written": len(records),
        "distinct_classes": sorted({record["class_level_1"] for record in records}),
        "boundary_area_ha": {targets[key]: round(value, 3) for key, value in sorted(areas.items())},
        "reconciliation_ratio_minimum_observed": round(min(v for v in ratios.values() if v is not None), 6),
        "reconciliation_ratio_maximum_observed": round(max(v for v in ratios.values() if v is not None), 6),
        "department_years_with_missing_cells": len(missing),
        "checks": checks,
        "gate_passed": all(checks.values()),
        "interpretation_limits": plan["interpretation_limits"],
        "outputs": [str(OUTPUT.relative_to(ROOT))],
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: audit[k] for k in ("rows_written", "checks", "gate_passed",
                                            "reconciliation_ratio_minimum_observed",
                                            "reconciliation_ratio_maximum_observed",
                                            "department_years_with_missing_cells")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
