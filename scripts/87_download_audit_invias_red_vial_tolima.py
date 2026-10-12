"""Download and audit the official INVIAS road network for Tolima.

This script performs a source and geographic-linkage audit. Road proximity or
intersection is not interpreted as human pressure, ignition, access, mechanism,
or causality.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests
from pyproj import Transformer
from shapely import make_valid
from shapely.geometry import GeometryCollection, LineString, MultiLineString, mapping, shape
from shapely.ops import transform, unary_union


ROOT = Path(__file__).resolve().parents[1]
SERVICE = "https://hermes.invias.gov.co/arcgis/rest/services/OpenData/ServiciosOpenData/FeatureServer"
LAYER = f"{SERVICE}/0"
QUERY = f"{LAYER}/query"
RAW_DIR = ROOT / "data" / "raw" / "invias_red_vial_2026-09-06"
RAW_METADATA = RAW_DIR / "layer_0_metadata.json"
RAW_PAGES = RAW_DIR / "geojson_pages"
RAW_GEOJSON = RAW_DIR / "red_vial_nacional.geojson"
BOUNDARY_SOURCE = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
EVENT_SOURCE = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_august_2026.geojson"
TOLIMA_ROADS = ROOT / "data" / "interim" / "invias_red_vial_tolima_exact_2026-09-06.geojson"
EVENT_LINKAGE = ROOT / "data" / "interim" / "gwis_invias_red_vial_linkage_august_2026.csv"
OUTPUT = ROOT / "results" / "exploratory" / "audit_invias_red_vial_tolima_gwis.json"
TO_UTM18N = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True).transform
TO_WGS84 = Transformer.from_crs("EPSG:32618", "EPSG:4326", always_xy=True).transform


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def linear_parts(geometry):
    """Return only line components from an arbitrary intersection geometry."""
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        parts = []
        for member in geometry.geoms:
            parts.extend(linear_parts(member))
        return parts
    return []


def request_json(url: str, params: dict) -> tuple[dict, requests.Response]:
    response = requests.get(url, params=params, timeout=120)
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(json.dumps(payload["error"], ensure_ascii=False))
    return payload, response


def main() -> None:
    targets = [RAW_PAGES, RAW_GEOJSON, TOLIMA_ROADS, EVENT_LINKAGE, OUTPUT]
    existing = [str(path) for path in targets if path.exists()]
    if existing:
        raise FileExistsError(f"Refusing to overwrite existing output: {existing}")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    RAW_PAGES.mkdir()
    TOLIMA_ROADS.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    if RAW_METADATA.exists():
        metadata = json.loads(RAW_METADATA.read_text(encoding="utf-8-sig"))
    else:
        metadata, metadata_response = request_json(LAYER, {"f": "json"})
        RAW_METADATA.write_bytes(metadata_response.content)
    if metadata.get("geometryType") != "esriGeometryPolyline":
        raise RuntimeError(f"Unexpected geometry type: {metadata.get('geometryType')}")

    count_payload, _ = request_json(QUERY, {"where": "1=1", "returnCountOnly": "true", "f": "json"})
    source_count_reported = int(count_payload["count"])
    ids_payload, _ = request_json(QUERY, {"where": "1=1", "returnIdsOnly": "true", "f": "json"})
    object_ids = sorted(ids_payload.get("objectIds", []))
    if len(object_ids) != source_count_reported:
        raise RuntimeError(f"Received {len(object_ids)} object IDs of {source_count_reported} reported features")
    features = []
    page_size = 25
    for page_number, start in enumerate(range(0, len(object_ids), page_size), start=1):
        page_ids = object_ids[start:start + page_size]
        page, page_response = request_json(QUERY, {
            "objectIds": ",".join(map(str, page_ids)),
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "geojson",
        })
        page_path = RAW_PAGES / f"red_vial_page_{page_number:03d}.geojson"
        page_path.write_bytes(page_response.content)
        features.extend(page.get("features", []))
    collection = {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:4326"}}, "features": features}
    RAW_GEOJSON.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
    if len(features) != source_count_reported:
        raise RuntimeError(f"Downloaded {len(features)} of {source_count_reported} reported features")

    boundary_collection = json.loads(BOUNDARY_SOURCE.read_text(encoding="utf-8-sig"))
    tolima_wgs84 = unary_union([make_valid(shape(feature["geometry"])) for feature in boundary_collection["features"]])
    tolima_utm = transform(TO_UTM18N, tolima_wgs84)

    invalid_before = 0
    repaired = 0
    empty_geometries = 0
    geometry_types = Counter()
    source_records = []
    for feature in features:
        geometry = shape(feature["geometry"])
        geometry_types[geometry.geom_type] += 1
        if geometry.is_empty:
            empty_geometries += 1
            continue
        if not geometry.is_valid:
            invalid_before += 1
            geometry = make_valid(geometry)
            repaired += 1
        source_records.append({
            "properties": dict(feature.get("properties", {})),
            "geometry_utm": transform(TO_UTM18N, geometry),
        })

    clipped_records = []
    for record in source_records:
        intersection = record["geometry_utm"].intersection(tolima_utm)
        parts = linear_parts(intersection)
        if not parts:
            continue
        clipped_geometry = unary_union(parts)
        clipped_records.append({
            "properties": record["properties"],
            "geometry_utm": clipped_geometry,
            "length_inside_tolima_m": float(clipped_geometry.length),
        })

    road_union_utm = unary_union([record["geometry_utm"] for record in clipped_records])
    sum_feature_length_m = sum(record["length_inside_tolima_m"] for record in clipped_records)
    union_length_m = float(road_union_utm.length)
    tolima_features = []
    for record in clipped_records:
        properties = dict(record["properties"])
        properties["length_inside_tolima_m"] = round(record["length_inside_tolima_m"], 6)
        tolima_features.append({
            "type": "Feature",
            "properties": properties,
            "geometry": mapping(transform(TO_WGS84, record["geometry_utm"])),
        })
    TOLIMA_ROADS.write_text(
        json.dumps({"type": "FeatureCollection", "features": tolima_features}, ensure_ascii=False),
        encoding="utf-8",
    )

    event_collection = json.loads(EVENT_SOURCE.read_text(encoding="utf-8"))
    linkage_rows = []
    for feature in event_collection["features"]:
        event_id = str(feature["properties"]["fire_id"])
        event_utm = transform(TO_UTM18N, make_valid(shape(feature["geometry"]))).intersection(tolima_utm)
        intersecting_features = []
        positive_length_features = []
        lengths = []
        for record in clipped_records:
            if not record["geometry_utm"].intersects(event_utm):
                continue
            intersecting_features.append(record)
            line_length = float(record["geometry_utm"].intersection(event_utm).length)
            if line_length > 0:
                positive_length_features.append(record)
                lengths.append(line_length)
        roads_inside_event = road_union_utm.intersection(event_utm)
        min_distance = float(event_utm.distance(road_union_utm)) if not road_union_utm.is_empty else None
        linkage_rows.append({
            "fire_id": event_id,
            "road_source_features_intersecting": len(intersecting_features),
            "road_source_features_with_positive_length_inside": len(positive_length_features),
            "sum_feature_road_length_inside_event_m": round(sum(lengths), 6),
            "union_road_length_inside_event_m": round(float(roads_inside_event.length), 6),
            "minimum_distance_event_to_road_network_m": round(min_distance, 6) if min_distance is not None else None,
        })
    linkage_rows.sort(key=lambda row: row["fire_id"])
    with EVENT_LINKAGE.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(linkage_rows[0]))
        writer.writeheader()
        writer.writerows(linkage_rows)

    field_metadata = metadata.get("fields", [])
    field_names = [field["name"] for field in field_metadata]
    null_counts = {
        name: sum(feature.get("properties", {}).get(name) is None for feature in features)
        for name in field_names
    }
    object_id_field = metadata["objectIdField"]
    object_ids = [feature.get("properties", {}).get(object_id_field) for feature in features]
    globalids = [feature.get("properties", {}).get("globalid") for feature in features]
    categorical_fields = ["key", "territorial", "administrador", "amv", "superficie", "calzada", "fuente", "estado"]
    categorical_counts = {
        name: dict(Counter(str(feature.get("properties", {}).get(name)) for feature in features))
        for name in categorical_fields if name in field_names
    }
    date_fields = [field["name"] for field in field_metadata if field.get("type") == "esriFieldTypeDate"]
    date_ranges = {}
    for name in date_fields:
        values = [feature.get("properties", {}).get(name) for feature in features]
        present = sorted(value for value in values if value is not None)
        date_ranges[name] = {"non_null_count": len(present), "minimum_as_exposed": present[0] if present else None, "maximum_as_exposed": present[-1] if present else None}

    output = {
        "audit_date": "2026-09-06",
        "access_time_utc": datetime.now(timezone.utc).isoformat(),
        "inference_class": "source and exact geographic linkage audit",
        "official_service": {
            "feature_service": SERVICE,
            "layer_url": LAYER,
            "query_url": QUERY,
            "service_description_as_exposed": metadata.get("serviceDescription"),
            "layer_name": metadata.get("name"),
            "capabilities": metadata.get("capabilities"),
            "geometry_type": metadata.get("geometryType"),
            "service_extent": metadata.get("extent"),
            "max_record_count": metadata.get("maxRecordCount"),
            "supported_query_formats": metadata.get("supportedQueryFormats"),
            "raw_metadata_file": str(RAW_METADATA.relative_to(ROOT)),
            "raw_metadata_sha256": sha256(RAW_METADATA),
            "raw_geojson_file": str(RAW_GEOJSON.relative_to(ROOT)),
            "raw_geojson_sha256": sha256(RAW_GEOJSON),
            "requested_output_crs": "EPSG:4326",
            "geojson_crs_as_exposed": collection.get("crs"),
        },
        "source_audit": {
            "reported_feature_count": source_count_reported,
            "downloaded_feature_count": len(features),
            "field_count": len(field_metadata),
            "fields": [{key: field.get(key) for key in ("name", "alias", "type", "nullable", "length")} for field in field_metadata],
            "null_counts": null_counts,
            "date_ranges_as_exposed": date_ranges,
            "categorical_counts_as_exposed": categorical_counts,
            "duplicate_non_null_object_ids": len([value for value, count in Counter(object_ids).items() if value is not None and count > 1]),
            "duplicate_non_null_globalids": len([value for value, count in Counter(globalids).items() if value is not None and count > 1]),
            "geometry_type_counts": dict(geometry_types),
            "empty_geometries": empty_geometries,
            "invalid_geometries_before_repair": invalid_before,
            "geometries_repaired_with_make_valid": repaired,
        },
        "tolima_exact_intersection": {
            "boundary_file": str(BOUNDARY_SOURCE.relative_to(ROOT)),
            "boundary_sha256": sha256(BOUNDARY_SOURCE),
            "method": "Exact line-polygon intersection in EPSG:32618 after service output in EPSG:4326",
            "source_features_with_positive_linear_intersection": len(clipped_records),
            "sum_feature_lengths_inside_tolima_m": sum_feature_length_m,
            "union_network_length_inside_tolima_m": union_length_m,
            "overlap_removed_by_union_m": sum_feature_length_m - union_length_m,
            "file": str(TOLIMA_ROADS.relative_to(ROOT)),
            "sha256": sha256(TOLIMA_ROADS),
        },
        "gwis_linkage": {
            "event_source": str(EVENT_SOURCE.relative_to(ROOT)),
            "event_source_sha256": sha256(EVENT_SOURCE),
            "event_count": len(linkage_rows),
            "events_intersecting_at_least_one_road_feature": sum(row["road_source_features_intersecting"] > 0 for row in linkage_rows),
            "events_with_positive_road_length_inside": sum(row["union_road_length_inside_event_m"] > 0 for row in linkage_rows),
            "events_at_zero_distance_from_network": sum(row["minimum_distance_event_to_road_network_m"] == 0 for row in linkage_rows),
            "linkage_type": "Exact geographic intersection and minimum Euclidean distance in EPSG:32618",
            "file": str(EVENT_LINKAGE.relative_to(ROOT)),
            "sha256": sha256(EVENT_LINKAGE),
        },
        "interpretation_limit": "The layer is the road network exposed by the INVIAS service. Feature sums can double-count coincident representations, so union length is also reported. Road distance or intersection does not establish human presence, ignition source, accessibility at the event date, mechanism, association, or causality.",
    }
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "national_features": len(features),
        "fields": len(field_metadata),
        "tolima_features": len(clipped_records),
        "tolima_union_length_km": union_length_m / 1000,
        "gwis_events": len(linkage_rows),
        "events_intersecting_roads": output["gwis_linkage"]["events_intersecting_at_least_one_road_feature"],
        "invalid_geometries": invalid_before,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
