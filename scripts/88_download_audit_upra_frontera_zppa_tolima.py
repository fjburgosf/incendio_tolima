"""Download and audit official UPRA territorial geometries for Tolima.

This script treats the agricultural frontier and ZPPA as planning
instruments.  It does not interpret either layer as observed land use,
agricultural activity, ignition source, burned area, or causal exposure.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

import requests
from pyproj import Transformer
from shapely import make_valid
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union


ROOT = Path(__file__).resolve().parents[1]
AUDIT_DATE = "2026-09-06"
RAW_DIR = ROOT / "data" / "raw" / "upra" / f"territorial_layers_tolima_{AUDIT_DATE}_v2"
BOUNDARY_SOURCE = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
EVENT_SOURCE = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_august_2026.geojson"
FRONTIER_INTERIM = ROOT / "data" / "interim" / "upra_agricultural_frontier_tolima.geojson"
FRONTIER_LINKAGE = ROOT / "data" / "interim" / "gwis_upra_agricultural_frontier_linkage_august_2026.csv"
ZPPA_INTERIM = ROOT / "data" / "interim" / "upra_zppa_tolima.geojson"
ZPPA_LINKAGE = ROOT / "data" / "interim" / "gwis_upra_zppa_linkage_august_2026.csv"
OUTPUT = ROOT / "results" / "exploratory" / "audit_upra_frontier_zppa_tolima_gwis_linkage.json"

SERVICES_DIRECTORY = "https://geoservicios.upra.gov.co/arcgis/rest/services/ordenamiento_productivo"
FRONTIER_LAYER = (
    "https://geoservicios.upra.gov.co/arcgis/rest/services/ordenamiento_productivo/"
    "frontera_agricola_frontera_agricola_condicionada/MapServer/0"
)
ZPPA_LAYER = (
    "https://geoservicios.upra.gov.co/arcgis/rest/services/ordenamiento_productivo/"
    "Zonas_Proteccion_Produccion_Alimentos_Tolima/MapServer/0"
)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Tolima-fire-source-audit/1.0"})
TO_UTM18N = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True).transform
TO_WGS84 = Transformer.from_crs("EPSG:32618", "EPSG:4326", always_xy=True).transform
MIN_AREA_HA = 0.01


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def write_response(path: Path, response: requests.Response) -> None:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite raw response: {path}")
    path.write_bytes(response.content)


def get_and_save(url: str, path: Path, params: dict[str, str]) -> requests.Response:
    response = SESSION.get(url, params=params, timeout=120)
    response.raise_for_status()
    write_response(path, response)
    return response


def area_ha(geometry) -> float:
    return float(geometry.area / 10_000.0) if not geometry.is_empty else 0.0


def download_layer(
    layer_url: str,
    prefix: str,
    where: str,
) -> tuple[dict, list[dict], dict]:
    """Download a feature layer as immutable response pages."""
    metadata_response = get_and_save(layer_url, RAW_DIR / f"{prefix}_layer_metadata.json", {"f": "json"})
    metadata = metadata_response.json()
    if "error" in metadata:
        query_response = get_and_save(
            f"{layer_url}/query",
            RAW_DIR / f"{prefix}_query_attempt.json",
            {"f": "json", "where": where, "returnIdsOnly": "true"},
        )
        return metadata, [], {
            "available": False,
            "metadata_error": metadata["error"],
            "query_response": query_response.json(),
        }

    ids_response = get_and_save(
        f"{layer_url}/query",
        RAW_DIR / f"{prefix}_object_ids.json",
        {"f": "json", "where": where, "returnIdsOnly": "true"},
    )
    ids_json = ids_response.json()
    if "error" in ids_json:
        return metadata, [], {"available": False, "query_error": ids_json["error"]}
    object_ids = sorted(ids_json.get("objectIds", []))
    oid_field = ids_json.get("objectIdFieldName") or "objectid"

    features: list[dict] = []
    # Keep the encoded objectIds parameter below conservative IIS URL limits.
    page_size = min(int(metadata.get("maxRecordCount") or 2000), 100)
    page_files = []
    for page_number, start in enumerate(range(0, len(object_ids), page_size), start=1):
        page_ids = object_ids[start : start + page_size]
        page_path = RAW_DIR / f"{prefix}_features_page_{page_number:03d}.geojson"
        response = get_and_save(
            f"{layer_url}/query",
            page_path,
            {
                "f": "geojson",
                "objectIds": ",".join(str(value) for value in page_ids),
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": "4326",
                "returnTrueCurves": "false",
            },
        )
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"ArcGIS query failed for {prefix} page {page_number}: {payload['error']}")
        features.extend(payload.get("features", []))
        page_files.append({
            "file": str(page_path.relative_to(ROOT)),
            "sha256": sha256(page_path),
            "feature_count": len(payload.get("features", [])),
        })

    return metadata, features, {
        "available": True,
        "where": where,
        "object_id_field": oid_field,
        "server_object_id_count": len(object_ids),
        "downloaded_feature_count": len(features),
        "page_size": page_size,
        "page_files": page_files,
    }


def audit_geometry(features: list[dict], tolima_utm, type_field: str | None) -> tuple[list[dict], dict]:
    records = []
    invalid_before = 0
    empty_before = 0
    empty_after_clip = 0
    for feature in features:
        geometry = shape(feature["geometry"])
        if geometry.is_empty:
            empty_before += 1
            continue
        if not geometry.is_valid:
            invalid_before += 1
            geometry = make_valid(geometry)
        geometry_utm = transform(TO_UTM18N, geometry)
        clipped = geometry_utm.intersection(tolima_utm)
        if clipped.is_empty or area_ha(clipped) < MIN_AREA_HA:
            empty_after_clip += 1
            continue
        properties = dict(feature.get("properties", {}))
        records.append({
            "properties": properties,
            "geometry_utm": clipped,
            "type": str(properties.get(type_field, "NO APLICA")) if type_field else "NO APLICA",
        })
    union_utm = unary_union([record["geometry_utm"] for record in records]) if records else None
    types = {}
    for value in sorted({record["type"] for record in records}):
        members = [record["geometry_utm"] for record in records if record["type"] == value]
        types[value] = {
            "record_count": len(members),
            "union_area_inside_tolima_ha": area_ha(unary_union(members)),
        }
    return records, {
        "input_feature_count": len(features),
        "retained_record_count": len(records),
        "invalid_geometries_before_make_valid": invalid_before,
        "empty_geometries_before": empty_before,
        "records_below_0_01_ha_after_exact_tolima_clip": empty_after_clip,
        "all_retained_geometries_valid_after_processing": all(record["geometry_utm"].is_valid for record in records),
        "sum_clipped_record_area_ha": sum(area_ha(record["geometry_utm"]) for record in records),
        "union_area_inside_tolima_ha": area_ha(union_utm) if union_utm else 0.0,
        "union_fraction_of_tolima": area_ha(union_utm) / area_ha(tolima_utm) if union_utm else 0.0,
        "type_summary": types,
    }


def write_interim(path: Path, records: list[dict], source_label: str) -> dict:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite interim output: {path}")
    output_features = []
    for record in records:
        properties = dict(record["properties"])
        properties["audit_source"] = source_label
        properties["area_inside_tolima_ha_computed"] = area_ha(record["geometry_utm"])
        output_features.append({
            "type": "Feature",
            "properties": properties,
            "geometry": mapping(transform(TO_WGS84, record["geometry_utm"])),
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "FeatureCollection", "features": output_features}, ensure_ascii=False), encoding="utf-8")
    return {"file": str(path.relative_to(ROOT)), "sha256": sha256(path), "feature_count": len(output_features)}


def link_events(
    records: list[dict],
    event_features: list[dict],
    tolima_utm,
    path: Path,
    type_field: str | None,
) -> dict:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite linkage output: {path}")
    total_union = unary_union([record["geometry_utm"] for record in records])
    by_type = {
        value: unary_union([record["geometry_utm"] for record in records if record["type"] == value])
        for value in sorted({record["type"] for record in records})
    }
    rows = []
    for feature in event_features:
        event_utm = transform(TO_UTM18N, make_valid(shape(feature["geometry"]))).intersection(tolima_utm)
        event_area = area_ha(event_utm)
        overlap_area = area_ha(event_utm.intersection(total_union))
        row = {
            "fire_id": feature["properties"]["fire_id"],
            "event_area_inside_tolima_ha": round(event_area, 6),
            "instrument_union_overlap_ha": round(overlap_area, 6),
            "instrument_union_fraction_of_event": overlap_area / event_area if event_area else None,
        }
        if type_field:
            for value, geometry in by_type.items():
                safe_name = "".join(character.lower() if character.isalnum() else "_" for character in value).strip("_")
                row[f"overlap_{safe_name}_ha"] = round(area_ha(event_utm.intersection(geometry)), 6)
        rows.append(row)
    rows.sort(key=lambda row: str(row["fire_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return {
        "file": str(path.relative_to(ROOT)),
        "sha256": sha256(path),
        "event_count": len(rows),
        "events_with_at_least_0_01_ha_overlap": sum(row["instrument_union_overlap_ha"] >= MIN_AREA_HA for row in rows),
        "events_fully_inside_at_99_percent": sum((row["instrument_union_fraction_of_event"] or 0) >= 0.99 for row in rows),
        "events_outside_at_1_percent_or_less": sum((row["instrument_union_fraction_of_event"] or 0) <= 0.01 for row in rows),
    }


def main() -> None:
    for target in (RAW_DIR, FRONTIER_INTERIM, FRONTIER_LINKAGE, ZPPA_INTERIM, ZPPA_LINKAGE, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing target: {target}")
    RAW_DIR.mkdir(parents=True)

    directory_response = get_and_save(SERVICES_DIRECTORY, RAW_DIR / "services_directory.json", {"f": "json"})
    services_directory = directory_response.json()

    boundary_collection = json.loads(BOUNDARY_SOURCE.read_text(encoding="utf-8-sig"))
    tolima_wgs84 = unary_union([make_valid(shape(feature["geometry"])) for feature in boundary_collection["features"]])
    tolima_utm = transform(TO_UTM18N, tolima_wgs84)
    event_collection = json.loads(EVENT_SOURCE.read_text(encoding="utf-8"))

    frontier_metadata, frontier_features, frontier_download = download_layer(
        FRONTIER_LAYER,
        "frontier",
        "cod_depart='73'",
    )
    if not frontier_download["available"]:
        raise RuntimeError(f"Current agricultural-frontier service was unavailable: {frontier_download}")
    get_and_save(FRONTIER_LAYER + "/iteminfo", RAW_DIR / "frontier_iteminfo.json", {"f": "json"})
    get_and_save(FRONTIER_LAYER + "/metadata", RAW_DIR / "frontier_metadata.xml", {"f": "xml"})
    frontier_records, frontier_geometry = audit_geometry(frontier_features, tolima_utm, "tipo_frontera")
    frontier_interim = write_interim(FRONTIER_INTERIM, frontier_records, "UPRA current MapServer")
    frontier_linkage = link_events(
        frontier_records,
        event_collection["features"],
        tolima_utm,
        FRONTIER_LINKAGE,
        "tipo_frontera",
    )

    zppa_metadata, zppa_features, zppa_download = download_layer(ZPPA_LAYER, "zppa", "1=1")
    zppa_section: dict = {
        "layer_url": ZPPA_LAYER,
        "listed_in_current_ordering_services_directory": any(
            service.get("name", "").endswith("Zonas_Proteccion_Produccion_Alimentos_Tolima")
            for service in services_directory.get("services", [])
        ),
        "download": zppa_download,
    }
    if zppa_download["available"]:
        zppa_records, zppa_geometry = audit_geometry(zppa_features, tolima_utm, None)
        zppa_section.update({
            "geometry_audit": zppa_geometry,
            "interim": write_interim(ZPPA_INTERIM, zppa_records, "UPRA ZPPA MapServer"),
            "gwis_linkage": link_events(
                zppa_records,
                event_collection["features"],
                tolima_utm,
                ZPPA_LINKAGE,
                None,
            ),
        })
    else:
        zppa_section["status"] = (
            "NO DISPONIBLE CON LA EVIDENCIA CONSULTADA. The formerly documented public layer "
            "returned an ArcGIS service-not-started error and was absent from the current folder listing."
        )

    result = {
        "audit_date": AUDIT_DATE,
        "inference_class": "source and exact geographic linkage audit",
        "boundary": {"file": str(BOUNDARY_SOURCE.relative_to(ROOT)), "sha256": sha256(BOUNDARY_SOURCE)},
        "events": {"file": str(EVENT_SOURCE.relative_to(ROOT)), "sha256": sha256(EVENT_SOURCE), "count": len(event_collection["features"])},
        "agricultural_frontier": {
            "layer_url": FRONTIER_LAYER,
            "layer_name": frontier_metadata.get("name"),
            "geometry_type": frontier_metadata.get("geometryType"),
            "service_spatial_reference": frontier_metadata.get("extent", {}).get("spatialReference"),
            "query_output_spatial_reference": "EPSG:4326 requested explicitly",
            "declared_generation_spatial_reference": "MAGNA-SIRGAS 2018 Origen Nacional 9377",
            "fields": [{"name": field.get("name"), "type": field.get("type")} for field in frontier_metadata.get("fields", [])],
            "download": frontier_download,
            "geometry_audit": frontier_geometry,
            "municipality_attribute_count": len({str(record["properties"].get("cod_dane_mpio", "")) for record in frontier_records}),
            "municipality_name_count": len({str(record["properties"].get("municipio", "")) for record in frontier_records}),
            "attribute_type_counts": dict(Counter(record["type"] for record in frontier_records)),
            "interim": frontier_interim,
            "gwis_linkage": frontier_linkage,
            "interpretation_limit": (
                "The layer is a planning boundary for permitted, promoted, conditioned, restricted, or prohibited agricultural activities. "
                "It is not observed 2026 land use, crop presence, agricultural practice, ignition, independent burned area, or causal exposure."
            ),
        },
        "zppa": zppa_section,
        "linkage_classification": {
            "agricultural_frontier_to_tolima": "exact geographic polygon intersection",
            "agricultural_frontier_to_gwis": "exact geographic polygon intersection after clipping to Tolima",
            "zppa_to_gwis": "exact geographic polygon intersection only if the public feature service was available",
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "frontier_features": frontier_download["downloaded_feature_count"],
        "frontier_types": result["agricultural_frontier"]["attribute_type_counts"],
        "frontier_union_area_tolima_ha": frontier_geometry["union_area_inside_tolima_ha"],
        "frontier_events_with_overlap": frontier_linkage["events_with_at_least_0_01_ha_overlap"],
        "zppa_available": zppa_download["available"],
        "zppa_status": zppa_section.get("status"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
