"""Audit georeferencing and pre-specified pixel quality for historical NASA burn products.

This script performs no comparison with GWIS and makes no claim of validation.
It verifies the HDF grid metadata, transforms the Tolima municipal boundary into
the declared native sinusoidal grid, applies the pre-specified centre-in-polygon
clip rule, and tabulates product-specific pixel-quality counts.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from pyhdf.SD import SD, SDC
from pyproj import CRS, Transformer
from shapely import contains_xy, make_valid
from shapely.geometry import box, shape
from shapely.ops import transform as transform_geometry
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[1]
ACCESS_DATE = "2026-09-07"
RAW_DIR = ROOT / "data" / "raw" / "nasa" / f"burned_area_historical_{ACCESS_DATE}"
BOUNDARY_SOURCE = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
PLAN_SOURCE = ROOT / "configs" / "nasa_burned_area_pixel_qc_plan_v1.json"
OUTPUT = ROOT / "results" / "exploratory" / f"audit_nasa_burned_area_hdf_georeference_pixel_qc_{ACCESS_DATE}_v2.json"
SUMMARY = ROOT / "data" / "interim" / "nasa_burned_area_historical_pixel_summary_v2.csv"

EXPECTED_YEARS = (2018, 2020, 2022, 2024, 2025)
EXPECTED_PRODUCTS = {
    "MCD64A1": "061",
    "VNP64A1": "002",
}
EXPECTED_LAYERS = {"Burn Date", "Burn Date Uncertainty", "QA", "First Day", "Last Day"}
FILENAME_PATTERN = re.compile(
    r"^(MCD64A1|VNP64A1)\.A(\d{4})(\d{3})\.h10v08\.(061|002)\..+\.hdf$"
)
FLOAT_PAIR = re.compile(r"\(([-+0-9.Ee]+),\s*([-+0-9.Ee]+)\)")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def attribute_text(value: object) -> str:
    return str(value).replace("\x00", "")


def required_match(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text)
    if not match:
        raise RuntimeError(f"Missing {label} in StructMetadata.0")
    return match.group(1)


def parse_struct_metadata(attributes: dict[str, object]) -> dict[str, object]:
    if "StructMetadata.0" not in attributes:
        raise RuntimeError("HDF lacks StructMetadata.0")
    text = attribute_text(attributes["StructMetadata.0"])
    upper_left_match = FLOAT_PAIR.search(required_match(r"UpperLeftPointMtrs=(\([^\)]+\))", text, "upper-left extent"))
    lower_right_match = FLOAT_PAIR.search(required_match(r"LowerRightMtrs=(\([^\)]+\))", text, "lower-right extent"))
    if not upper_left_match or not lower_right_match:
        raise RuntimeError("Could not parse native projected extents")
    proj_parameter_text = required_match(r"ProjParams=\(([^\)]*)\)", text, "projection parameters")
    first_parameter = float(proj_parameter_text.split(",")[0])
    return {
        "grid_name": required_match(r'GridName="([^"]+)"', text, "grid name"),
        "x_dimension": int(required_match(r"XDim=(\d+)", text, "X dimension")),
        "y_dimension": int(required_match(r"YDim=(\d+)", text, "Y dimension")),
        "upper_left_m": [float(upper_left_match.group(1)), float(upper_left_match.group(2))],
        "lower_right_m": [float(lower_right_match.group(1)), float(lower_right_match.group(2))],
        "projection": required_match(r"Projection=([^\n\r]+)", text, "projection"),
        "sphere_radius_m": first_parameter,
        "sphere_code": int(required_match(r"SphereCode=(-?\d+)", text, "sphere code")),
        "grid_origin": required_match(r"GridOrigin=([^\n\r]+)", text, "grid origin"),
    }


def parse_filename(path: Path) -> dict[str, object]:
    match = FILENAME_PATTERN.match(path.name)
    if not match:
        raise RuntimeError(f"Unexpected HDF filename: {path.name}")
    product, year_text, day_text, version = match.groups()
    if EXPECTED_PRODUCTS[product] != version:
        raise RuntimeError(f"Unexpected version in {path.name}")
    start_date = date(int(year_text), 1, 1) + timedelta(days=int(day_text) - 1)
    if start_date.month != 8 or start_date.day != 1:
        raise RuntimeError(f"Expected an August monthly granule, found {start_date.isoformat()} in {path.name}")
    return {"product": product, "year": int(year_text), "ordinal_start_day": int(day_text), "version": version}


def load_tolima_native(native_crs: CRS) -> tuple[object, dict[str, object]]:
    with BOUNDARY_SOURCE.open(encoding="utf-8-sig") as handle:
        source = json.load(handle)
    source_geometries = [shape(feature["geometry"]) for feature in source["features"]]
    geometry = unary_union(source_geometries)
    if geometry.is_empty or not geometry.is_valid:
        raise RuntimeError("Tolima boundary union is empty or invalid")
    transformer = Transformer.from_crs("EPSG:4326", native_crs, always_xy=True)
    transformed = transform_geometry(transformer.transform, geometry)
    if transformed.is_empty:
        raise RuntimeError("Transformed Tolima geometry is empty")
    repair_applied = not transformed.is_valid
    transformed_area_before_repair = float(transformed.area)
    if repair_applied:
        repaired = make_valid(transformed)
        polygonal_parts = [
            part for part in getattr(repaired, "geoms", (repaired,))
            if part.geom_type in {"Polygon", "MultiPolygon"} and not part.is_empty
        ]
        if not polygonal_parts:
            raise RuntimeError("Projection validity repair returned no polygonal Tolima geometry")
        transformed = unary_union(polygonal_parts)
    if transformed.is_empty or not transformed.is_valid:
        raise RuntimeError("Transformed Tolima geometry remains empty or invalid after repair")
    relative_area_change = (
        abs(float(transformed.area) - transformed_area_before_repair) / transformed_area_before_repair
        if transformed_area_before_repair > 0 else 0.0
    )
    if relative_area_change > 1e-10:
        raise RuntimeError(f"Projection validity repair changed Tolima area too much: {relative_area_change}")
    return transformed, {
        "source_feature_count": len(source_geometries),
        "invalid_source_feature_count": sum(not feature.is_valid for feature in source_geometries),
        "source_union_valid": geometry.is_valid,
        "transformed_valid_before_repair": not repair_applied,
        "repair_applied": repair_applied,
        "transformed_area_square_m_before_repair": transformed_area_before_repair,
        "transformed_area_square_m_after_repair": float(transformed.area),
        "relative_area_change_after_repair": relative_area_change,
        "maximum_allowed_relative_area_change": 1e-10,
    }


def read_layers(path: Path) -> tuple[dict[str, np.ndarray], dict[str, object], list[str]]:
    handle = SD(str(path), SDC.READ)
    try:
        metadata = parse_struct_metadata(handle.attributes())
        datasets = handle.datasets()
        layer_names = sorted(datasets)
        if set(layer_names) != EXPECTED_LAYERS:
            raise RuntimeError(f"Unexpected HDF layers in {path.name}: {layer_names}")
        layers: dict[str, np.ndarray] = {}
        for name in EXPECTED_LAYERS:
            _, dimensions, _, index = datasets[name]
            expected_dimensions = (metadata["y_dimension"], metadata["x_dimension"])
            if tuple(dimensions) != expected_dimensions:
                raise RuntimeError(f"Unexpected dimensions for {name} in {path.name}: {dimensions}")
            dataset = handle.select(index)
            try:
                layers[name] = np.asarray(dataset.get())
            finally:
                dataset.endaccess()
        return layers, metadata, layer_names
    finally:
        handle.end()


def target_mask(metadata: dict[str, object], tolima_native: object) -> tuple[np.ndarray, dict[str, object]]:
    x_dimension = int(metadata["x_dimension"])
    y_dimension = int(metadata["y_dimension"])
    ulx, uly = (float(value) for value in metadata["upper_left_m"])
    lrx, lry = (float(value) for value in metadata["lower_right_m"])
    dx = (lrx - ulx) / x_dimension
    dy = (lry - uly) / y_dimension
    if not (dx > 0 and dy < 0):
        raise RuntimeError(f"Unexpected grid orientation: dx={dx}, dy={dy}")
    tile = box(ulx, lry, lrx, uly)
    if not tile.intersects(tolima_native):
        raise RuntimeError("The h10v08 native footprint does not intersect transformed Tolima")
    if not tile.contains(tolima_native):
        raise RuntimeError("The h10v08 native footprint does not fully contain transformed Tolima")
    minx, miny, maxx, maxy = tolima_native.bounds
    column_start = max(0, int(np.floor((minx - ulx) / dx)))
    column_end = min(x_dimension, int(np.ceil((maxx - ulx) / dx)))
    row_start = max(0, int(np.floor((uly - maxy) / abs(dy))))
    row_end = min(y_dimension, int(np.ceil((uly - miny) / abs(dy))))
    if column_start >= column_end or row_start >= row_end:
        raise RuntimeError("Transformed Tolima bounding window has no raster cells")
    mask = np.zeros((y_dimension, x_dimension), dtype=bool)
    columns = np.arange(column_start, column_end)
    x_centres = ulx + (columns + 0.5) * dx
    for row_block_start in range(row_start, row_end, 128):
        row_block_end = min(row_block_start + 128, row_end)
        rows = np.arange(row_block_start, row_block_end)
        y_centres = uly + (rows + 0.5) * dy
        x_grid, y_grid = np.meshgrid(x_centres, y_centres)
        mask[row_block_start:row_block_end, column_start:column_end] = contains_xy(tolima_native, x_grid, y_grid)
    return mask, {
        "native_tile_bounds_m": [ulx, lry, lrx, uly],
        "native_tolima_bounds_m": [minx, miny, maxx, maxy],
        "clip_window_row_start_inclusive": row_start,
        "clip_window_row_end_exclusive": row_end,
        "clip_window_column_start_inclusive": column_start,
        "clip_window_column_end_exclusive": column_end,
        "tolima_cell_centres": int(mask.sum()),
        "cell_width_m": dx,
        "cell_height_m": abs(dy),
        "native_cell_area_square_m": dx * abs(dy),
        "tile_intersects_transformed_tolima": True,
        "tile_contains_transformed_tolima": True,
    }


def category_counts(values: np.ndarray, mask: np.ndarray) -> dict[str, int]:
    selected = values[mask]
    return {
        "burned_positive": int(np.count_nonzero(selected > 0)),
        "unburned_zero": int(np.count_nonzero(selected == 0)),
        "unmapped_negative_1": int(np.count_nonzero(selected == -1)),
        "water_negative_2": int(np.count_nonzero(selected == -2)),
        "other_values": int(np.count_nonzero(~((selected > 0) | (selected == 0) | (selected == -1) | (selected == -2)))),
    }


def audit_file(path: Path, tolima_native: object) -> tuple[dict[str, object], dict[str, object]]:
    filename = parse_filename(path)
    layers, metadata, layer_names = read_layers(path)
    mask, grid_audit = target_mask(metadata, tolima_native)
    burn_date = layers["Burn Date"]
    qa = layers["QA"].astype(np.uint8, copy=False)
    primary = mask & (burn_date > 0) & ((qa & 1) == 1) & (((qa >> 1) & 1) == 1)
    sensitivity = primary & (((qa >> 2) & 1) == 0)
    burned_any = mask & (burn_date > 0)
    land_bit = (qa & 1) == 1
    valid_data_bit = ((qa >> 1) & 1) == 1
    shortened_bit = ((qa >> 2) & 1) == 1
    cell_area_ha = float(grid_audit["native_cell_area_square_m"]) / 10000.0
    record = {
        **filename,
        "file": path.name,
        "sha256": sha256(path),
        "layer_names": layer_names,
        "metadata": metadata,
        "grid_audit": grid_audit,
        "burn_date_category_counts_inside_tolima": category_counts(burn_date, mask),
        "qa_counts_inside_tolima": {
            "land_bit_0_equal_1": int(np.count_nonzero(mask & land_bit)),
            "valid_data_bit_1_equal_1": int(np.count_nonzero(mask & valid_data_bit)),
            "shortened_mapping_period_bit_2_equal_1": int(np.count_nonzero(mask & shortened_bit)),
        },
        "burned_positive_but_land_bit_0_not_1": int(np.count_nonzero(burned_any & ~land_bit)),
        "burned_positive_but_valid_data_bit_1_not_1": int(np.count_nonzero(burned_any & ~valid_data_bit)),
        "primary_burned_pixel_count": int(np.count_nonzero(primary)),
        "primary_burned_native_grid_hectares": float(np.count_nonzero(primary) * cell_area_ha),
        "primary_burned_with_shortened_mapping_period_count": int(np.count_nonzero(primary & shortened_bit)),
        "sensitivity_excluding_shortened_mapping_period_pixel_count": int(np.count_nonzero(sensitivity)),
        "sensitivity_excluding_shortened_mapping_period_native_grid_hectares": float(np.count_nonzero(sensitivity) * cell_area_ha),
    }
    csv_record = {
        "product": filename["product"],
        "version": filename["version"],
        "year": filename["year"],
        "file": path.name,
        "tolima_cell_centres": grid_audit["tolima_cell_centres"],
        "native_cell_area_square_m": grid_audit["native_cell_area_square_m"],
        "burned_positive": record["burn_date_category_counts_inside_tolima"]["burned_positive"],
        "unburned_zero": record["burn_date_category_counts_inside_tolima"]["unburned_zero"],
        "unmapped_negative_1": record["burn_date_category_counts_inside_tolima"]["unmapped_negative_1"],
        "water_negative_2": record["burn_date_category_counts_inside_tolima"]["water_negative_2"],
        "primary_burned_pixel_count": record["primary_burned_pixel_count"],
        "primary_burned_native_grid_hectares": record["primary_burned_native_grid_hectares"],
        "primary_shortened_mapping_period_count": record["primary_burned_with_shortened_mapping_period_count"],
        "sensitivity_pixel_count": record["sensitivity_excluding_shortened_mapping_period_pixel_count"],
        "sensitivity_native_grid_hectares": record["sensitivity_excluding_shortened_mapping_period_native_grid_hectares"],
    }
    return record, csv_record


def main() -> None:
    for target in (OUTPUT, SUMMARY):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    if not RAW_DIR.is_dir():
        raise FileNotFoundError(f"Missing raw HDF directory: {RAW_DIR}")
    plan = json.loads(PLAN_SOURCE.read_text(encoding="utf-8"))
    paths = sorted(RAW_DIR.glob("*.hdf"))
    if len(paths) != len(EXPECTED_YEARS) * len(EXPECTED_PRODUCTS):
        raise RuntimeError(f"Expected ten HDF files, found {len(paths)}")
    native_crs = CRS.from_proj4("+proj=sinu +R=6371007.181 +units=m +no_defs")
    tolima_native, tolima_transform_audit = load_tolima_native(native_crs)
    records: list[dict[str, object]] = []
    csv_records: list[dict[str, object]] = []
    seen_pairs: set[tuple[str, int]] = set()
    for path in paths:
        record, csv_record = audit_file(path, tolima_native)
        key = (str(record["product"]), int(record["year"]))
        if key in seen_pairs:
            raise RuntimeError(f"Duplicate product-year file: {key}")
        seen_pairs.add(key)
        records.append(record)
        csv_records.append(csv_record)
    expected_pairs = {(product, year) for product in EXPECTED_PRODUCTS for year in EXPECTED_YEARS}
    if seen_pairs != expected_pairs:
        raise RuntimeError(f"Product-year coverage mismatch: {seen_pairs ^ expected_pairs}")
    grid_fingerprints = {
        (
            record["metadata"]["x_dimension"],
            record["metadata"]["y_dimension"],
            tuple(record["metadata"]["upper_left_m"]),
            tuple(record["metadata"]["lower_right_m"]),
            record["metadata"]["projection"],
            record["metadata"]["sphere_radius_m"],
            record["metadata"]["grid_origin"],
        )
        for record in records
    }
    if len(grid_fingerprints) != 1:
        raise RuntimeError("HDF grid metadata are not identical across retained product-year files")
    fieldnames = list(csv_records[0])
    with SUMMARY.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(sorted(csv_records, key=lambda row: (int(row["year"]), str(row["product"]))))
    output = {
        "audit_date": ACCESS_DATE,
        "inference_class": "descriptive georeferencing and pixel-quality audit",
        "plan_source": str(PLAN_SOURCE.relative_to(ROOT)),
        "boundary_source": str(BOUNDARY_SOURCE.relative_to(ROOT)),
        "raw_source_directory": str(RAW_DIR.relative_to(ROOT)),
        "native_crs_definition": "+proj=sinu +R=6371007.181 +units=m +no_defs",
        "tolima_projection_transform_audit": tolima_transform_audit,
        "pre_execution_acceptance_criteria": {
            "exactly_one_product_year_file": True,
            "all_required_layers_and_declared_grid_metadata": True,
            "all_grids_identical_ignoring_product_specific_grid_name": True,
            "native_h10v08_tile_contains_transformed_tolima": True,
            "centre_in_polygon_clip_rule": plan["scope"]["target_clip_rule"],
            "native_cell_area_rule": plan["scope"]["cell_area_rule"],
            "projection_validity_repair_rule": plan["scope"]["projection_validity_repair_rule"],
        },
        "acceptance_result": "pass",
        "records": sorted(records, key=lambda record: (int(record["year"]), str(record["product"]))),
        "summary_csv": str(SUMMARY.relative_to(ROOT)),
        "interpretation_limit": "These are product-specific pixel counts after a documented geographic clip. They do not establish ground truth, validate GWIS, or support inference about August 2026.",
    }
    OUTPUT.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "summary": str(SUMMARY), "acceptance_result": "pass"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
