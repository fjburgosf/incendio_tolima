"""Download and audit GWIS NRT burned-area event polygons for Tolima.

The current-situation viewer exposes the NRT layer through WMS. MapServer's
Mapbox vector-tile output is used to preserve geometries and attributes.
The download is tiled because the polygon layer has a scale threshold.

The product is derived from NASA FIRMS thermal anomalies. It is therefore an
event/perimeter estimate, not an independent validation of FIRMS detections.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import mapbox_vector_tile
import requests
from pyproj import Geod
from shapely.geometry import Point, mapping, shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[1]
ACCESS_DATE = "2026-09-06"
VIEWER_URL = "https://forest-fire.emergency.copernicus.eu/apps/gwis_current_situation/"
BUNDLE_URL = VIEWER_URL + "static/js/app.bundle-2.11.4.js"
TECHNICAL_URL = "https://gwis.jrc.ec.europa.eu/about-gwis/technical-background/burnt-areas"
WMS_URL = "https://maps.effis.emergency.copernicus.eu/gwis"
LAYER = "nrt.ba.poly"
TIME_RANGE = "2026-08-01/2026-08-31"
BOUNDARY_SOURCE = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
POINT_SOURCE = ROOT / "data" / "interim" / "ideam_daily_heat_points_tolima_august_2026.csv"
RAW_DIR = ROOT / "data" / "raw" / f"gwis_nrt_burned_area_{ACCESS_DATE}"
PARTIAL_DIR = ROOT / "data" / "raw" / f".gwis_nrt_burned_area_{ACCESS_DATE}.partial"
INTERIM = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_august_2026.geojson"
OUTPUT = ROOT / "results" / "exploratory" / "audit_gwis_nrt_burned_area_tolima_august_2026.json"
TILE_DEGREES = 0.25
QUERY_PADDING_DEGREES = 0.05
TILE_PIXELS = 1024
MAX_WORKERS = 4
GEOD = Geod(ellps="WGS84")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def request(url: str, *, params: dict[str, object] | None = None) -> requests.Response:
    response = requests.get(
        url,
        params=params,
        timeout=(20, 120),
        headers={"User-Agent": "tolima-fire-data-audit/1.0"},
    )
    response.raise_for_status()
    return response


def transform_coordinates(value: object, tile_bbox: tuple[float, float, float, float], extent: int):
    west, south, east, north = tile_bbox
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
        return [transform_coordinates(item, tile_bbox, extent) for item in value]
    raise TypeError(f"Unexpected MVT coordinate value {type(value)!r}")


def geodesic_area_ha(geometry) -> float:
    if geometry.is_empty:
        return 0.0
    return abs(GEOD.geometry_area_perimeter(geometry)[0]) / 10_000.0


def build_tiles(bounds: tuple[float, float, float, float]) -> list[dict[str, object]]:
    west, south, east, north = bounds
    west -= QUERY_PADDING_DEGREES
    south -= QUERY_PADDING_DEGREES
    east += QUERY_PADDING_DEGREES
    north += QUERY_PADDING_DEGREES
    columns = math.ceil((east - west) / TILE_DEGREES)
    rows = math.ceil((north - south) / TILE_DEGREES)
    tiles = []
    for row in range(rows):
        for column in range(columns):
            tile_west = west + column * TILE_DEGREES
            tile_south = south + row * TILE_DEGREES
            tile_east = min(tile_west + TILE_DEGREES, east)
            tile_north = min(tile_south + TILE_DEGREES, north)
            tiles.append(
                {
                    "row": row,
                    "column": column,
                    "bbox": (tile_west, tile_south, tile_east, tile_north),
                    "filename": f"tile_r{row:02d}_c{column:02d}.mvt",
                }
            )
    return tiles


def download_tile(tile: dict[str, object]) -> dict[str, object]:
    bbox = tile["bbox"]
    params = {
        "service": "WMS",
        "version": "1.1.1",
        "request": "GetMap",
        "layers": LAYER,
        "styles": "",
        "srs": "EPSG:4326",
        "bbox": ",".join(f"{value:.12f}" for value in bbox),
        "width": TILE_PIXELS,
        "height": TILE_PIXELS,
        "format": "application/vnd.mapbox-vector-tile",
        "transparent": "true",
        "time": TIME_RANGE,
    }
    response = request(WMS_URL, params=params)
    content_type = response.headers.get("Content-Type", "")
    if "application/vnd.mapbox-vector-tile" not in content_type:
        raise RuntimeError(
            f"Unexpected response for {tile['filename']}: {content_type} {response.text[:300]}"
        )
    decoded = mapbox_vector_tile.decode(response.content)
    feature_count = len(decoded.get(LAYER, {}).get("features", []))
    return {
        **tile,
        "request_url": response.url,
        "content": response.content,
        "content_type": content_type,
        "feature_count": feature_count,
        "sha256": sha256_bytes(response.content),
    }


def main() -> None:
    for target in (RAW_DIR, PARTIAL_DIR, INTERIM, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {target}")

    with BOUNDARY_SOURCE.open(encoding="utf-8-sig") as handle:
        boundary_geojson = json.load(handle)
    municipality_records = [
        (feature["properties"]["mun_nombre"], shape(feature["geometry"]))
        for feature in boundary_geojson["features"]
    ]
    tolima_geometry = unary_union([geometry for _, geometry in municipality_records])
    tolima_bbox = tuple(float(value) for value in tolima_geometry.bounds)

    PARTIAL_DIR.mkdir(parents=True)
    tiles_dir = PARTIAL_DIR / "tiles"
    tiles_dir.mkdir()

    source_responses = {
        "viewer_html": request(VIEWER_URL),
        "viewer_bundle": request(BUNDLE_URL),
        "technical_background": request(TECHNICAL_URL),
        "wms_capabilities": request(
            WMS_URL,
            params={"service": "WMS", "version": "1.1.1", "request": "GetCapabilities"},
        ),
    }
    source_paths = {
        "viewer_html": PARTIAL_DIR / "gwis_current_situation.html",
        "viewer_bundle": PARTIAL_DIR / "gwis_current_situation_app_bundle_2.11.4.js",
        "technical_background": PARTIAL_DIR / "gwis_burnt_areas_technical_background.html",
        "wms_capabilities": PARTIAL_DIR / "gwis_wms_getcapabilities_1.1.1.xml",
    }
    for key, response in source_responses.items():
        source_paths[key].write_bytes(response.content)

    bundle_text = source_responses["viewer_bundle"].text
    capabilities_text = source_responses["wms_capabilities"].text
    technical_text = source_responses["technical_background"].text
    mechanism_checks = {
        "viewer_bundle_exposes_wms_endpoint": WMS_URL in bundle_text,
        "viewer_bundle_exposes_nrt_layer": 'wms_layer:"nrt.ba"' in bundle_text,
        "capabilities_exposes_polygon_layer": f"<Name>{LAYER}</Name>" in capabilities_text,
        "capabilities_exposes_mvt_format": "application/vnd.mapbox-vector-tile" in capabilities_text,
        "technical_page_names_nasa_firms": "NASA FIRMS" in technical_text,
        "technical_page_names_modis_and_viirs_thermal_anomalies": (
            "thermal anomalies from MODIS and VIIRS" in technical_text
        ),
    }
    if not all(mechanism_checks.values()):
        raise RuntimeError(f"GWIS mechanism checks failed: {mechanism_checks}")

    tiles = build_tiles(tolima_bbox)
    completed_tiles: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(download_tile, tile): tile for tile in tiles}
        for future in as_completed(futures):
            downloaded = future.result()
            (tiles_dir / str(downloaded["filename"])).write_bytes(downloaded.pop("content"))
            completed_tiles.append(downloaded)
    completed_tiles.sort(key=lambda item: (int(item["row"]), int(item["column"])))

    raw_index = {
        "access_date": ACCESS_DATE,
        "viewer_url": VIEWER_URL,
        "technical_background_url": TECHNICAL_URL,
        "wms_url": WMS_URL,
        "layer": LAYER,
        "time_range": TIME_RANGE,
        "tile_degrees": TILE_DEGREES,
        "tile_pixels": TILE_PIXELS,
        "query_padding_degrees": QUERY_PADDING_DEGREES,
        "tolima_bbox": tolima_bbox,
        "mechanism_checks": mechanism_checks,
        "source_files": {
            key: {
                "filename": path.name,
                "request_url": source_responses[key].url,
                "content_type": source_responses[key].headers.get("Content-Type"),
                "bytes": len(source_responses[key].content),
                "sha256": sha256_bytes(source_responses[key].content),
            }
            for key, path in source_paths.items()
        },
        "tiles": [
            {key: value for key, value in tile.items() if key != "bbox"}
            | {"bbox": list(tile["bbox"])}
            for tile in completed_tiles
        ],
    }
    (PARTIAL_DIR / "download_index.json").write_text(
        json.dumps(raw_index, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    PARTIAL_DIR.rename(RAW_DIR)

    geometry_parts: dict[str, list] = defaultdict(list)
    property_versions: dict[str, set[tuple[tuple[str, object], ...]]] = defaultdict(set)
    tile_occurrences = Counter()
    for tile in completed_tiles:
        tile_path = RAW_DIR / "tiles" / str(tile["filename"])
        decoded = mapbox_vector_tile.decode(tile_path.read_bytes())
        layer = decoded.get(LAYER, {"extent": 4096, "features": []})
        extent = int(layer.get("extent", 4096))
        for feature in layer.get("features", []):
            properties = feature.get("properties", {})
            fire_id = str(properties.get("fire_id") or properties.get("id") or feature.get("id"))
            lonlat_coordinates = transform_coordinates(
                feature["geometry"]["coordinates"], tuple(tile["bbox"]), extent
            )
            geometry = shape(
                {"type": feature["geometry"]["type"], "coordinates": lonlat_coordinates}
            )
            if not geometry.is_valid:
                geometry = geometry.buffer(0)
            geometry_parts[fire_id].append(geometry)
            property_versions[fire_id].add(tuple(sorted(properties.items())))
            tile_occurrences[fire_id] += 1

    merged_events = []
    property_conflicts = {}
    for fire_id, parts in geometry_parts.items():
        versions = property_versions[fire_id]
        if len(versions) != 1:
            property_conflicts[fire_id] = [dict(version) for version in versions]
        properties = dict(next(iter(versions)))
        geometry = unary_union(parts)
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty or not geometry.intersects(tolima_geometry):
            continue
        tolima_intersection = geometry.intersection(tolima_geometry)
        municipality_overlaps = []
        for municipality, municipal_geometry in municipality_records:
            overlap = geometry.intersection(municipal_geometry)
            overlap_area_ha = geodesic_area_ha(overlap)
            if overlap_area_ha > 0.01:
                municipality_overlaps.append(
                    {"municipality": municipality, "overlap_area_ha": round(overlap_area_ha, 3)}
                )
        municipality_overlaps.sort(key=lambda item: item["overlap_area_ha"], reverse=True)
        reported_area = float(properties["area"]) if properties.get("area") not in (None, "") else None
        geometry_area = geodesic_area_ha(geometry)
        merged_events.append(
            {
                "fire_id": fire_id,
                "initial_datetime_as_exposed": properties.get("initialdate"),
                "final_datetime_as_exposed": properties.get("finaldate"),
                "reported_area_ha": reported_area,
                "geometry_area_ha": round(geometry_area, 3),
                "tolima_intersection_area_ha": round(geodesic_area_ha(tolima_intersection), 3),
                "municipality_overlaps": municipality_overlaps,
                "tile_occurrences": tile_occurrences[fire_id],
                "geometry": geometry,
            }
        )
    merged_events.sort(key=lambda item: (str(item["initial_datetime_as_exposed"]), item["fire_id"]))

    with POINT_SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        point_rows = list(csv.DictReader(handle))
    point_records = []
    for row in point_rows:
        local_datetime = datetime.strptime(row["datetime_local_utc_minus_5"], "%Y-%m-%d %H:%M")
        point_records.append(
            {
                "datetime_utc": local_datetime + timedelta(hours=5),
                "point": Point(float(row["longitude"]), float(row["latitude"])),
            }
        )

    exposed_initial_matches = 0
    events_with_temporal_spatial_points = 0
    for event in merged_events:
        initial = datetime.strptime(str(event["initial_datetime_as_exposed"]), "%Y-%m-%d %H:%M:%S")
        final = datetime.strptime(str(event["final_datetime_as_exposed"]), "%Y-%m-%d %H:%M:%S")
        temporal_spatial_points = [
            record
            for record in point_records
            if initial <= record["datetime_utc"] <= final
            and event["geometry"].covers(record["point"])
        ]
        event["tolima_firms_points_within_event_time_and_geometry"] = len(temporal_spatial_points)
        if temporal_spatial_points:
            events_with_temporal_spatial_points += 1
        if any(record["datetime_utc"] == initial for record in temporal_spatial_points):
            exposed_initial_matches += 1

    features = []
    for event in merged_events:
        geometry = event.pop("geometry")
        features.append(
            {
                "type": "Feature",
                "id": event["fire_id"],
                "properties": event,
                "geometry": mapping(geometry),
            }
        )
    feature_collection = {
        "type": "FeatureCollection",
        "name": "GWIS NRT burned-area event estimates intersecting Tolima during August 2026",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }
    INTERIM.write_text(json.dumps(feature_collection, ensure_ascii=False), encoding="utf-8")

    area_differences = []
    for feature in features:
        reported = feature["properties"]["reported_area_ha"]
        geometry_area = feature["properties"]["geometry_area_ha"]
        if reported and reported > 0:
            area_differences.append(abs(geometry_area - reported) / reported)
    municipality_event_counts = Counter()
    for feature in features:
        for overlap in feature["properties"]["municipality_overlaps"]:
            municipality_event_counts[overlap["municipality"]] += 1

    audit = {
        "audit_date": ACCESS_DATE,
        "inference_class": "descriptive data-source audit",
        "source": {
            "viewer_url": VIEWER_URL,
            "technical_background_url": TECHNICAL_URL,
            "wms_url": WMS_URL,
            "layer": LAYER,
            "time_range": TIME_RANGE,
            "raw_directory": str(RAW_DIR.relative_to(ROOT)),
            "raw_download_index_sha256": sha256_file(RAW_DIR / "download_index.json"),
        },
        "download": {
            "tile_count": len(completed_tiles),
            "tiles_with_features": sum(tile["feature_count"] > 0 for tile in completed_tiles),
            "total_tile_feature_occurrences": sum(tile_occurrences.values()),
            "unique_fire_ids_in_query_tiles": len(geometry_parts),
            "property_conflict_count": len(property_conflicts),
            "property_conflicts": property_conflicts,
        },
        "tolima_events": {
            "event_count": len(features),
            "event_ids": [feature["properties"]["fire_id"] for feature in features],
            "event_count_by_municipality_intersection": dict(sorted(municipality_event_counts.items())),
            "reported_area_sum_ha_not_independent": round(
                sum(feature["properties"]["reported_area_ha"] or 0 for feature in features), 3
            ),
            "tolima_intersection_geometry_area_sum_ha": round(
                sum(feature["properties"]["tolima_intersection_area_ha"] for feature in features), 3
            ),
            "events_with_tolima_firms_points_inside_time_and_geometry": events_with_temporal_spatial_points,
            "events_with_an_exact_initial_minute_match_after_local_plus_5h": exposed_initial_matches,
            "median_relative_difference_geometry_vs_reported_area": (
                sorted(area_differences)[len(area_differences) // 2] if area_differences else None
            ),
            "max_relative_difference_geometry_vs_reported_area": max(area_differences, default=None),
        },
        "mechanism_checks": mechanism_checks,
        "interpretation": {
            "what_it_is": (
                "GWIS NRT event polygons and area estimates derived by spatial-temporal clustering "
                "of MODIS and VIIRS thermal anomalies supplied by NASA FIRMS."
            ),
            "what_it_is_not": (
                "It is not an independent burned-area observation, official incident inventory, "
                "cause record, severity product, or negative fire label."
            ),
            "allowed_use_at_this_gate": (
                "Candidate event definition and perimeter/area estimate for descriptive or "
                "associational analyses, with explicit same-source measurement coupling and "
                "later comparison against independent optical scars or official perimeters."
            ),
            "area_unit_verification": (
                "The MVT field is named area without a unit. It is operationalized as hectares "
                "because its values are numerically checked against WGS84 geodesic polygon area. "
                "This is a mechanism-based verification, not reliance on the field name alone."
            ),
        },
        "outputs": {
            "interim_geojson": str(INTERIM.relative_to(ROOT)),
            "interim_geojson_sha256": sha256_file(INTERIM),
        },
    }
    OUTPUT.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(audit, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
