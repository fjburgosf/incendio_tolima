"""Construct audited CHIRPS antecedent precipitation features by department.

The script reads raw global monthly COGs listed in the retrieval manifest. It
uses each precipitation grid cell centre once, assigns it to one of eight
pre-specified departments by polygon containment, and produces area weighted
mean monthly precipitation depths. May through July totals are 92 day seasonal
accumulations and are never labelled exact 90 day accumulations.
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

import numpy as np
import tifffile
from shapely import contains_xy
from shapely.geometry import shape


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "configs" / "regional_preseason_prediction_plan_v1.json"
BOUNDARY_PATH = ROOT / "data" / "raw" / "boundaries" / "geoBoundaries-COL-ADM1.geojson"
MANIFEST_PATH = ROOT / "data" / "raw" / "chirps_v3" / "monthly_global_cogs_2012_2026" / "manifest_v1.json"
OUTPUT_PATH = ROOT / "data" / "processed" / "chirps_regional_antecedent_precipitation_v1.csv"
AUDIT_PATH = ROOT / "results" / "primary" / "audit_chirps_regional_antecedent_precipitation_v1.json"
NODATA = -9999.0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def normalized(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]+", "", text.upper())


def read_departments(target_names: list[str]) -> dict[str, object]:
    payload = json.loads(BOUNDARY_PATH.read_text(encoding="utf-8"))
    targets = {normalized(name): name for name in target_names}
    geometries: dict[str, list] = {name: [] for name in target_names}
    for feature in payload["features"]:
        matches = {targets[normalized(value)] for value in feature.get("properties", {}).values() if normalized(value) in targets}
        if len(matches) > 1:
            raise RuntimeError("A boundary feature matches multiple target departments")
        if matches:
            geometries[matches.pop()].append(shape(feature["geometry"]))
    missing = [name for name, parts in geometries.items() if len(parts) != 1]
    if missing:
        raise RuntimeError(f"Each target department must have one boundary feature, failing: {missing}")
    return {name: parts[0] for name, parts in geometries.items()}


def grid_assignment(reference_path: Path, departments: dict[str, object]) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, int], dict[str, object]]:
    with tifffile.TiffFile(reference_path) as container:
        page = container.pages[0]
        scale = page.tags[33550].value
        tiepoint = page.tags[33922].value
        rows, columns = page.shape
    scale_x, scale_y = float(scale[0]), float(scale[1])
    tie_col, tie_row, _, tie_lon, tie_lat, _ = [float(value) for value in tiepoint[:6]]
    west, east, south, north = -78.0, -73.0, 0.9, 5.9
    col_centres = tie_lon + (np.arange(columns, dtype=np.float64) + 0.5 - tie_col) * scale_x
    row_centres = tie_lat - (np.arange(rows, dtype=np.float64) + 0.5 - tie_row) * scale_y
    col_index = np.flatnonzero((col_centres >= west) & (col_centres <= east))
    row_index = np.flatnonzero((row_centres >= south) & (row_centres <= north))
    if len(col_index) == 0 or len(row_index) == 0:
        raise RuntimeError("The regional bounding box has no CHIRPS grid centres")
    lon_grid, lat_grid = np.meshgrid(col_centres[col_index], row_centres[row_index])
    assignment = np.full(lon_grid.shape, "", dtype=object)
    cell_count = {}
    for name, geometry in departments.items():
        inside = contains_xy(geometry, lon_grid, lat_grid)
        if np.any((assignment != "") & inside):
            raise RuntimeError(f"Department polygons overlap on CHIRPS centres, first department: {name}")
        assignment[inside] = name
        cell_count[name] = int(np.count_nonzero(inside))
    if any(count == 0 for count in cell_count.values()):
        raise RuntimeError(f"A target department has no CHIRPS centres: {cell_count}")
    weights = np.cos(np.deg2rad(lat_grid))
    metadata = {
        "full_shape": [int(rows), int(columns)],
        "subset_shape": [int(len(row_index)), int(len(col_index))],
        "pixel_scale_degrees": [scale_x, scale_y],
        "centre_rule": "centre within exact GeoBoundaries department polygon",
        "assignment_cell_count": cell_count,
        "subset_unassigned_cells": int(np.count_nonzero(assignment == "")),
    }
    return row_index, col_index, assignment, cell_count, {"weights": weights, "metadata": metadata}


def read_month(path: Path, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    with tifffile.TiffFile(path) as container:
        values = container.pages[0].asarray()
    if values.ndim != 2:
        raise RuntimeError(f"CHIRPS primary page is not two dimensional: {path.name}")
    return np.asarray(values[np.ix_(rows, cols)], dtype=np.float64)


def main() -> None:
    if OUTPUT_PATH.exists() or AUDIT_PATH.exists():
        raise FileExistsError("Processed CHIRPS output or audit exists and must not be overwritten")
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    expected = [(year, month) for year in plan["temporal_design"]["seasons"] for month in (5, 6, 7)]
    records = {(int(row["year"]), int(row["month"])): ROOT / row["file"] for row in manifest["records"]}
    if set(records) != set(expected):
        missing = sorted(set(expected) - set(records))
        extra = sorted(set(records) - set(expected))
        raise RuntimeError(f"CHIRPS manifest does not match frozen season months, missing {missing[:3]}, extra {extra[:3]}")
    if any(not path.exists() for path in records.values()):
        raise RuntimeError("A CHIRPS file declared in the manifest is absent")
    departments = read_departments(plan["geography"]["departments"])
    reference = records[(2026, 7)]
    rows, cols, assignment, cell_count, grid = grid_assignment(reference, departments)
    weights = grid["weights"]
    monthly: dict[tuple[str, int, int], dict[str, float]] = {}
    source_checksums = []
    for year, month in expected:
        path = records[(year, month)]
        values = read_month(path, rows, cols)
        for name in departments:
            member = assignment == name
            valid = member & np.isfinite(values) & (values != NODATA)
            if int(np.count_nonzero(valid)) != cell_count[name]:
                raise RuntimeError(f"Missing or nodata CHIRPS values for {name} {year}-{month:02d}")
            monthly[(name, year, month)] = {
                "mean_mm": float(np.average(values[valid], weights=weights[valid])),
                "valid_cell_count": int(np.count_nonzero(valid)),
            }
        source_checksums.append({"year": year, "month": month, "file": str(path.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(path)})
    output_rows = []
    for year in plan["temporal_design"]["seasons"]:
        for name in plan["geography"]["departments"]:
            may = monthly[(name, year, 5)]["mean_mm"]
            june = monthly[(name, year, 6)]["mean_mm"]
            july = monthly[(name, year, 7)]["mean_mm"]
            output_rows.append(
                {
                    "department": name,
                    "season": year,
                    "chirps_may_mean_mm": round(may, 6),
                    "chirps_june_mean_mm": round(june, 6),
                    "chirps_july_mean_mm": round(july, 6),
                    "chirps_may_july_mean_mm": round(may + june + july, 6),
                    "chirps_grid_cells": cell_count[name],
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
        "manifest": str(MANIFEST_PATH.relative_to(ROOT)).replace("\\", "/"),
        "source_files": source_checksums,
        "grid": grid["metadata"],
        "row_count": len(output_rows),
        "month_completeness": "three months per department season",
        "unit": "area weighted mean monthly precipitation depth in millimetres",
        "accumulation_definition": "May plus June plus July. This is a 92 day calendar accumulation, not exact rolling 90 day precipitation.",
        "output": {"path": str(OUTPUT_PATH.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(OUTPUT_PATH)},
        "limits": [
            "The COG is a global source grid. Departmental means depend on centre inclusion at 0.05 degrees.",
            "Precipitation is not soil moisture and does not identify fuel moisture or ignition cause.",
        ],
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(output_rows), "grid": grid["metadata"], "output": str(OUTPUT_PATH.relative_to(ROOT))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
