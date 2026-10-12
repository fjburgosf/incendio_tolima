"""Check historical GWIS event schema and geometry consistency before cohort use."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import mapbox_vector_tile
from pyproj import Geod
from shapely.geometry import shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[1]
ACCESS_DATE = "2026-09-07"
LAYER = "nrt.ba.poly"
RAW_DIR = ROOT / "data" / "raw" / f"gwis_historical_access_probe_{ACCESS_DATE}"
BOUNDARY_SOURCE = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
INTERIM = ROOT / "data" / "interim" / f"gwis_historical_event_schema_summary_{ACCESS_DATE}_v2.csv"
OUTPUT = ROOT / "results" / "exploratory" / f"audit_gwis_historical_schema_geometry_{ACCESS_DATE}_v2.json"
GEOD = Geod(ellps="WGS84")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def transform_coordinates(value: object, bbox: tuple[float, float, float, float], extent: int):
    west, south, east, north = bbox
    if (
        isinstance(value, list)
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    ):
        return [
            west + float(value[0]) / extent * (east - west),
            south + float(value[1]) / extent * (north - south),
        ]
    if isinstance(value, list):
        return [transform_coordinates(item, bbox, extent) for item in value]
    raise TypeError(f"Unexpected tile coordinate type {type(value)!r}")


def geodesic_area_ha(geometry) -> float:
    return abs(GEOD.geometry_area_perimeter(geometry)[0]) / 10_000.0 if not geometry.is_empty else 0.0


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def main() -> None:
    for target in (INTERIM, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {target}")
    with (RAW_DIR / "download_index.json").open(encoding="utf-8") as handle:
        raw_index = json.load(handle)
    with BOUNDARY_SOURCE.open(encoding="utf-8-sig") as handle:
        boundary = json.load(handle)
    tolima = unary_union([shape(feature["geometry"]) for feature in boundary["features"]])

    event_rows: list[dict[str, object]] = []
    season_audits: dict[str, object] = {}
    for season, summary in raw_index["season_summaries"].items():
        geometry_parts: dict[str, list] = defaultdict(list)
        property_versions: dict[str, set[tuple[tuple[str, str], ...]]] = defaultdict(set)
        tile_occurrences: dict[str, int] = defaultdict(int)
        for tile in summary["tiles"]:
            tile_path = RAW_DIR / season / "tiles" / tile["filename"]
            decoded = mapbox_vector_tile.decode(tile_path.read_bytes())
            layer = decoded.get(LAYER, {"extent": 4096, "features": []})
            extent = int(layer.get("extent", 4096))
            bbox = tuple(float(value) for value in tile["bbox"])
            for feature in layer.get("features", []):
                properties = feature.get("properties", {})
                event_id = str(properties.get("fire_id") or properties.get("id") or feature.get("id"))
                coordinates = transform_coordinates(feature["geometry"]["coordinates"], bbox, extent)
                geometry = shape({"type": feature["geometry"]["type"], "coordinates": coordinates})
                if not geometry.is_valid:
                    geometry = geometry.buffer(0)
                geometry_parts[event_id].append(geometry)
                property_versions[event_id].add(tuple(sorted((key, str(value)) for key, value in properties.items())))
                tile_occurrences[event_id] += 1

        retained = 0
        property_conflicts = 0
        relative_differences: list[float] = []
        for event_id, pieces in geometry_parts.items():
            geometry = unary_union(pieces)
            if not geometry.is_valid:
                geometry = geometry.buffer(0)
            if geometry.is_empty or not geometry.intersects(tolima):
                continue
            retained += 1
            versions = property_versions[event_id]
            property_conflicts += int(len(versions) != 1)
            properties = dict(next(iter(versions)))
            reported_area = float(properties["area"]) if properties.get("area") not in {None, ""} else None
            geometry_area = geodesic_area_ha(geometry)
            relative_difference = (
                abs(geometry_area - reported_area) / reported_area
                if reported_area is not None and reported_area > 0
                else None
            )
            if relative_difference is not None:
                relative_differences.append(relative_difference)
            event_rows.append(
                {
                    "season": season,
                    "fire_id": event_id,
                    "initialdate": properties.get("initialdate"),
                    "finaldate": properties.get("finaldate"),
                    "reported_area_ha": reported_area,
                    "geometry_area_ha": round(geometry_area, 6),
                    "relative_area_difference": relative_difference,
                    "tile_occurrences": tile_occurrences[event_id],
                    "property_conflict": len(versions) != 1,
                    "intersects_tolima": True,
                }
            )
        season_audits[season] = {
            "events_intersecting_tolima": retained,
            "property_conflict_count": property_conflicts,
            "relative_area_difference_median": percentile(relative_differences, 0.5),
            "relative_area_difference_p95": percentile(relative_differences, 0.95),
            "relative_area_difference_max": max(relative_differences) if relative_differences else None,
        }

    fieldnames = list(event_rows[0]) if event_rows else []
    with INTERIM.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(event_rows)
    observed_schemas = {
        season: data["property_names"]
        for season, data in raw_index["season_summaries"].items()
    }
    audit = {
        "audit_date": ACCESS_DATE,
        "inference_class": "descriptive comparability audit",
        "raw_input": str((RAW_DIR / "download_index.json").relative_to(ROOT)),
        "raw_input_sha256": sha256_file(RAW_DIR / "download_index.json"),
        "observed_property_names_by_season": observed_schemas,
        "identical_property_name_sets": len({tuple(values) for values in observed_schemas.values()}) == 1,
        "season_audits": season_audits,
        "outputs": {
            "event_summary": str(INTERIM.relative_to(ROOT)),
            "event_summary_sha256": sha256_file(INTERIM),
        },
        "interpretation": (
            "Matching property names and an area-geometry consistency diagnostic are necessary "
            "technical checks. They do not establish equal detection sensitivity, unchanged "
            "input sensors, event completeness, or causal comparability across years."
        ),
    }
    OUTPUT.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(audit, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
