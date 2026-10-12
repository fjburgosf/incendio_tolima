"""Sample Copernicus DEM GLO 90 at the frozen thermal event anchors."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import tifffile


ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / "data" / "interim" / "gwis_event_master_v1.csv"
RAW_DIR = ROOT / "data" / "raw" / "copernicus_dem_glo90_tolima_2026-09-06"
OUTPUT = ROOT / "data" / "interim" / "gwis_event_anchor_dem_glo90_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_gwis_event_anchor_dem_glo90_v1.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()



def haversine_m(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
    radius = 6_371_000.0
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    d_phi = phi_b - phi_a
    d_lambda = math.radians(lon_b - lon_a)
    value = math.sin(d_phi / 2) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


def load_tiles() -> list[dict]:
    tiles = []
    for manifest_path in sorted(RAW_DIR.glob("*_manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        request = manifest["request"]
        bbox = request["input"]["bounds"]["bbox"]
        filename = manifest["file"]
        path = RAW_DIR / filename
        image = tifffile.imread(path)
        if image.ndim != 3 or image.shape[2] != 2:
            raise RuntimeError(f"Unexpected DEM tile shape for {filename}: {image.shape}")
        height, width = image.shape[:2]
        west, south, east, north = map(float, bbox)
        xs = west + (np.arange(width) + 0.5) / width * (east - west)
        ys = north - (np.arange(height) + 0.5) / height * (north - south)
        valid = (image[:, :, 1] > 0) & np.isfinite(image[:, :, 0])
        if not valid.any():
            raise RuntimeError(f"No valid DEM cells in {filename}")
        tiles.append({
            "file": filename,
            "sha256": sha256(path),
            "bbox": [west, south, east, north],
            "xs": xs,
            "ys": ys,
            "dem": image[:, :, 0].astype(np.float64),
            "valid": valid,
        })
    if len(tiles) != 2:
        raise RuntimeError(f"Expected two DEM tiles, found {len(tiles)}")
    return tiles


def sample_nearest(tile: dict, longitude: float, latitude: float) -> dict | None:
    west, south, east, north = tile["bbox"]
    if not (west <= longitude <= east and south <= latitude <= north):
        return None
    col = int(np.abs(tile["xs"] - longitude).argmin())
    row = int(np.abs(tile["ys"] - latitude).argmin())
    if tile["valid"][row, col]:
        sample_lon = float(tile["xs"][col])
        sample_lat = float(tile["ys"][row])
        return {"row": row, "column": col, "longitude": sample_lon, "latitude": sample_lat,
                "elevation_m": float(tile["dem"][row, col]), "distance_m": haversine_m(longitude, latitude, sample_lon, sample_lat),
                "fallback_to_other_valid_pixel": False}
    valid_rows, valid_cols = np.where(tile["valid"])
    candidate_lons = tile["xs"][valid_cols]
    candidate_lats = tile["ys"][valid_rows]
    distances = (candidate_lons - longitude) ** 2 + (candidate_lats - latitude) ** 2
    position = int(distances.argmin())
    sample_row, sample_col = int(valid_rows[position]), int(valid_cols[position])
    sample_lon, sample_lat = float(tile["xs"][sample_col]), float(tile["ys"][sample_row])
    return {"row": sample_row, "column": sample_col, "longitude": sample_lon, "latitude": sample_lat,
            "elevation_m": float(tile["dem"][sample_row, sample_col]), "distance_m": haversine_m(longitude, latitude, sample_lon, sample_lat),
            "fallback_to_other_valid_pixel": True}


def main() -> None:
    for target in (OUTPUT, AUDIT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    tiles = load_tiles()
    with MASTER.open(encoding="utf-8-sig", newline="") as handle:
        master_rows = list(csv.DictReader(handle))
    if len(master_rows) != 62 or len({row["fire_id"] for row in master_rows}) != 62:
        raise RuntimeError("Unexpected event-master cardinality")
    rows = []
    outside = []
    for event in master_rows:
        lon, lat = float(event["anchor_longitude"]), float(event["anchor_latitude"])
        candidates = [(tile, sample_nearest(tile, lon, lat)) for tile in tiles]
        candidates = [(tile, sample) for tile, sample in candidates if sample is not None]
        if not candidates:
            outside.append(event["fire_id"])
            continue
        tile, sample = min(candidates, key=lambda pair: pair[1]["distance_m"])
        rows.append({
            "fire_id": event["fire_id"],
            "anchor_longitude": lon,
            "anchor_latitude": lat,
            "dem_tile_file": tile["file"],
            "dem_pixel_row_zero_based": sample["row"],
            "dem_pixel_column_zero_based": sample["column"],
            "dem_pixel_center_longitude": sample["longitude"],
            "dem_pixel_center_latitude": sample["latitude"],
            "anchor_to_dem_pixel_center_distance_m": sample["distance_m"],
            "dem_elevation_m": sample["elevation_m"],
            "dem_fallback_to_nearest_valid_pixel": sample["fallback_to_other_valid_pixel"],
        })
    if outside or len(rows) != 62:
        raise RuntimeError(f"DEM sample coverage failed. Outside anchors: {outside}")
    rows.sort(key=lambda row: row["fire_id"])
    with OUTPUT.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    distances = [float(row["anchor_to_dem_pixel_center_distance_m"]) for row in rows]
    elevations = [float(row["dem_elevation_m"]) for row in rows]
    audit = {
        "source": {"event_master": {"file": str(MASTER.relative_to(ROOT)), "sha256": sha256(MASTER)},
                   "dem_tiles": [{"file": tile["file"], "sha256": tile["sha256"], "bbox": tile["bbox"]} for tile in tiles]},
        "sampling_rule": "nearest valid centre of the API supplied Copernicus DEM GLO 90 grid to the frozen thermal anchor",
        "event_count": len(rows),
        "outside_dem_tile_anchor_ids": outside,
        "fallback_to_other_valid_pixel_count": sum(bool(row["dem_fallback_to_nearest_valid_pixel"]) for row in rows),
        "anchor_to_pixel_distance_m": {"minimum": min(distances), "median": float(np.median(distances)), "maximum": max(distances)},
        "elevation_m": {"minimum": min(elevations), "median": float(np.median(elevations)), "maximum": max(elevations)},
        "output": {"file": str(OUTPUT.relative_to(ROOT)), "sha256": sha256(OUTPUT)},
        "interpretation_limit": "The sampled value is a static Copernicus DEM GLO 90 digital surface elevation at an event anchor. It is not a ground-station observation, slope, aspect, terrain mechanism, ignition location, or causal variable.",
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "event_count": audit["event_count"],
        "fallback_count": audit["fallback_to_other_valid_pixel_count"],
        "max_anchor_to_pixel_distance_m": audit["anchor_to_pixel_distance_m"]["maximum"],
        "elevation_range_m": audit["elevation_m"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
