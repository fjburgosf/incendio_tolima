"""Build a matched spatial case control panel for Tolima August 2026.

Cases are active one kilometre VIIRS NRT cells. Controls are non active cells
sampled in the same municipality. The script creates an analytic sample only;
it does not fit a model or infer physical fire occurrence.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import tifffile
from pyproj import Transformer
from shapely import Point, STRtree
from shapely.geometry import box, shape
from shapely.ops import transform, unary_union
from shapely.prepared import prep


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "configs" / "spatial_occurrence_case_control_plan_v1.json"
DETECTIONS_PATH = ROOT / "data" / "interim" / "regional_fire_detections_labelled_v1.csv"
MUNICIPAL_PATH = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
UPRA_PATH = ROOT / "data" / "interim" / "upra_agricultural_frontier_tolima.geojson"
DEM_DIR = ROOT / "data" / "raw" / "copernicus_dem_glo90_tolima_2026-09-06"
OUTPUT_PATH = ROOT / "data" / "processed" / "tolima_spatial_case_control_panel_v2.csv"
AUDIT_PATH = ROOT / "results" / "primary" / "audit_tolima_spatial_case_control_panel_v2.json"
CRS_PROJECTED = "EPSG:3116"
CELL_METRES = 1000.0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def projected_municipalities() -> dict[str, object]:
    payload = json.loads(MUNICIPAL_PATH.read_text(encoding="utf-8"))
    forward = Transformer.from_crs("EPSG:4326", CRS_PROJECTED, always_xy=True).transform
    municipalities = {}
    for feature in payload["features"]:
        code = str(feature["properties"]["mun_codigo"])
        name = str(feature["properties"]["mun_nombre"])
        geometry = transform(forward, shape(feature["geometry"]))
        if code in municipalities:
            raise RuntimeError(f"Duplicated municipal code {code}")
        municipalities[code] = {"name": name, "geometry": geometry}
    if len(municipalities) != 47:
        raise RuntimeError(f"Expected 47 municipalities, found {len(municipalities)}")
    return municipalities


def municipal_for_point(point: Point, prepared: dict[str, object]) -> str | None:
    matched = [code for code, geometry in prepared.items() if geometry.contains(point)]
    if len(matched) > 1:
        raise RuntimeError("A grid centre is in more than one municipality")
    return matched[0] if matched else None


def cell_centre(cell_x: int, cell_y: int) -> tuple[float, float]:
    return (cell_x + 0.5) * CELL_METRES, (cell_y + 0.5) * CELL_METRES


def dem_tiles() -> list[dict]:
    records = []
    for path in sorted(DEM_DIR.glob("*.tif")):
        with tifffile.TiffFile(path) as document:
            page = document.pages[0]
            scale = page.tags["ModelPixelScaleTag"].value
            tie = page.tags["ModelTiepointTag"].value
            array = page.asarray()
        if array.ndim != 3 or array.shape[2] != 2:
            raise RuntimeError(f"Unexpected DEM band structure in {path.name}: {array.shape}")
        records.append({
            "path": path,
            "array": array,
            "lon0": float(tie[3]),
            "lat0": float(tie[4]),
            "pixel_lon": float(scale[0]),
            "pixel_lat": float(scale[1]),
        })
    if len(records) != 2:
        raise RuntimeError(f"Expected two DEM tiles, found {len(records)}")
    return records


def dem_value_and_slope(x: float, y: float, tiles: list[dict], inverse: Transformer) -> tuple[float, float] | None:
    longitude, latitude = inverse.transform(x, y)
    for tile in tiles:
        column = int(math.floor((longitude - tile["lon0"]) / tile["pixel_lon"]))
        row = int(math.floor((tile["lat0"] - latitude) / tile["pixel_lat"]))
        array = tile["array"]
        if not (1 <= row < array.shape[0] - 1 and 1 <= column < array.shape[1] - 1):
            continue
        window = array[row - 1:row + 2, column - 1:column + 2, :]
        elevation = float(window[1, 1, 0])
        mask = window[:, :, 1]
        values = window[:, :, 0]
        if not np.isfinite(values).all() or not np.all(mask > 0) or not math.isfinite(elevation):
            return None
        dx = tile["pixel_lon"] * 111_320.0 * math.cos(math.radians(latitude))
        dy = tile["pixel_lat"] * 110_574.0
        dzdx = (float(values[1, 2]) - float(values[1, 0])) / (2.0 * dx)
        dzdy = (float(values[2, 1]) - float(values[0, 1])) / (2.0 * dy)
        slope_deg = math.degrees(math.atan(math.sqrt(dzdx ** 2 + dzdy ** 2)))
        return elevation, slope_deg
    return None


def main() -> None:
    if OUTPUT_PATH.exists() or AUDIT_PATH.exists():
        raise FileExistsError("Spatial case control outputs exist and must not be overwritten")
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    seed = int(plan["matching"]["random_seed"])
    ratio = int(plan["matching"]["ratio_controls_per_case"])
    records = read_csv(DETECTIONS_PATH)
    case_records = [
        row for row in records
        if row["department"] == "Tolima" and int(row["season"]) == 2026 and row["processing_level"] == "NRT"
    ]
    if not case_records:
        raise RuntimeError("No Tolima August 2026 NRT records found")
    counts = defaultdict(int)
    for row in case_records:
        counts[(int(row["cell_x"]), int(row["cell_y"]))] += 1
    cases = sorted(counts)
    municipalities = projected_municipalities()
    prepared = {code: prep(record["geometry"]) for code, record in municipalities.items()}
    union_bounds = unary_union([record["geometry"] for record in municipalities.values()]).bounds
    min_x = int(math.floor(union_bounds[0] / CELL_METRES))
    max_x = int(math.floor(union_bounds[2] / CELL_METRES))
    min_y = int(math.floor(union_bounds[1] / CELL_METRES))
    max_y = int(math.floor(union_bounds[3] / CELL_METRES))
    candidates_by_municipality: dict[str, list[tuple[int, int]]] = defaultdict(list)
    candidate_municipality: dict[tuple[int, int], str] = {}
    for cell_x in range(min_x, max_x + 1):
        for cell_y in range(min_y, max_y + 1):
            x, y = cell_centre(cell_x, cell_y)
            municipality = municipal_for_point(Point(x, y), prepared)
            if municipality:
                key = (cell_x, cell_y)
                candidates_by_municipality[municipality].append(key)
                candidate_municipality[key] = municipality
    missing_cases = [key for key in cases if key not in candidate_municipality]
    if missing_cases:
        raise RuntimeError(f"Case cells outside unique municipal grid population: {len(missing_cases)}")
    cases_by_municipality: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for key in cases:
        cases_by_municipality[candidate_municipality[key]].append(key)
    rng = np.random.default_rng(seed)
    selected: list[dict] = []
    used_controls: set[tuple[int, int]] = set()
    reduced_strata = []
    stratum = 0
    for municipality in sorted(cases_by_municipality):
        available = [key for key in candidates_by_municipality[municipality] if key not in counts]
        required = ratio * len(cases_by_municipality[municipality])
        if len(available) < required:
            reduced_strata.append({"municipality": municipality, "available": len(available), "required": required})
        rng.shuffle(available)
        cursor = 0
        for case in sorted(cases_by_municipality[municipality]):
            stratum += 1
            selected.append({"cell_x": case[0], "cell_y": case[1], "case": 1, "stratum": stratum,
                             "municipality_code": municipality, "case_detection_count": counts[case]})
            take = min(ratio, len(available) - cursor)
            for control in available[cursor:cursor + take]:
                if control in used_controls:
                    raise RuntimeError("Control reuse detected")
                used_controls.add(control)
                selected.append({"cell_x": control[0], "cell_y": control[1], "case": 0, "stratum": stratum,
                                 "municipality_code": municipality, "case_detection_count": 0})
            cursor += take
    if reduced_strata:
        raise RuntimeError(f"Insufficient controls under frozen ratio: {reduced_strata}")

    forward = Transformer.from_crs("EPSG:4326", CRS_PROJECTED, always_xy=True).transform
    upra_payload = json.loads(UPRA_PATH.read_text(encoding="utf-8"))
    frontier_geometries = [transform(forward, shape(feature["geometry"])) for feature in upra_payload["features"]]
    frontier_tree = STRtree(frontier_geometries)
    tiles = dem_tiles()
    inverse = Transformer.from_crs(CRS_PROJECTED, "EPSG:4326", always_xy=True)
    output_rows = []
    excluded_dem = 0
    for row in selected:
        x, y = cell_centre(row["cell_x"], row["cell_y"])
        sample = dem_value_and_slope(x, y, tiles, inverse)
        if sample is None:
            excluded_dem += 1
            continue
        elevation, slope = sample
        cell = box(row["cell_x"] * CELL_METRES, row["cell_y"] * CELL_METRES,
                   (row["cell_x"] + 1) * CELL_METRES, (row["cell_y"] + 1) * CELL_METRES)
        candidate_indices = frontier_tree.query(cell, predicate="intersects")
        if len(candidate_indices) == 0:
            frontier_area = 0.0
        else:
            local_union = unary_union([frontier_geometries[int(index)] for index in candidate_indices])
            frontier_area = cell.intersection(local_union).area
        frontier_fraction = frontier_area / (CELL_METRES ** 2)
        output_rows.append({
            **row,
            "municipality_name": municipalities[row["municipality_code"]]["name"],
            "centre_easting_m": round(x, 3),
            "centre_northing_m": round(y, 3),
            "elevation_m": round(elevation, 6),
            "slope_degrees": round(slope, 6),
            "upra_frontier_fraction": round(frontier_fraction, 8),
        })
    retained_cases = sum(row["case"] for row in output_rows)
    retained_controls = sum(1 for row in output_rows if row["case"] == 0)
    if excluded_dem:
        raise RuntimeError(f"DEM sampling excluded {excluded_dem} selected rows")
    if retained_cases != len(cases) or retained_controls != ratio * len(cases):
        raise RuntimeError("Unexpected retained case control ratio")
    groups = defaultdict(list)
    for row in output_rows:
        groups[row["stratum"]].append(row)
    checks = {
        "case_count_matches_active_cells": retained_cases == len(cases),
        "all_cases_have_unique_municipality": all(row["municipality_code"] for row in output_rows if row["case"] == 1),
        "all_cases_have_finite_dem": all(math.isfinite(row["elevation_m"]) and math.isfinite(row["slope_degrees"]) for row in output_rows if row["case"] == 1),
        "all_controls_are_non_cases": all((row["cell_x"], row["cell_y"]) not in counts for row in output_rows if row["case"] == 0),
        "all_strata_have_one_case": all(sum(row["case"] for row in group) == 1 and len(group) == ratio + 1 for group in groups.values()),
        "all_controls_are_within_case_municipality": all(len({row["municipality_code"] for row in group}) == 1 for group in groups.values()),
        "no_control_reuse": len(used_controls) == retained_controls,
        "all_predictors_complete_after_eligibility_filters": all(
            math.isfinite(row["elevation_m"]) and math.isfinite(row["slope_degrees"]) and math.isfinite(row["upra_frontier_fraction"])
            for row in output_rows
        ),
    }
    if not all(checks.values()):
        raise RuntimeError(f"Case control acceptance criteria failed: {checks}")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)
    audit = {
        "script": Path(__file__).name,
        "plan": {"path": str(PLAN_PATH.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(PLAN_PATH)},
        "inputs": [
            {"path": str(path.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(path)}
            for path in [DETECTIONS_PATH, MUNICIPAL_PATH, UPRA_PATH]
        ] + [{"path": str(path.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(path)} for path in sorted(DEM_DIR.glob("*.tif"))],
        "active_detection_records": len(case_records),
        "active_one_kilometre_cells": len(cases),
        "eligible_grid_cells": len(candidate_municipality),
        "selected_cases": retained_cases,
        "selected_controls": retained_controls,
        "strata": len(groups),
        "control_ratio": ratio,
        "random_seed": seed,
        "dem_rows_excluded": excluded_dem,
        "checks": checks,
        "output": {"path": str(OUTPUT_PATH.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(OUTPUT_PATH)},
        "limits": plan["excluded_predictors"],
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cases": retained_cases, "controls": retained_controls, "strata": len(groups), "eligible_grid_cells": len(candidate_municipality), "checks": checks}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
