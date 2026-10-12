"""Audit exact RUNAP intersections with Tolima and GWIS event geometries.

This is a source and linkage audit. Protected-area overlap is not interpreted
as protection effectiveness, damage, exposure, mechanism, or causality.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections import Counter
from pathlib import Path

import shapefile
from pyproj import Transformer
from shapely import make_valid
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union


ROOT = Path(__file__).resolve().parents[1]
RUNAP_ZIP = ROOT / "data" / "raw" / "runap" / "runap_latest.zip"
BOUNDARY_SOURCE = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
EVENT_SOURCE = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_august_2026.geojson"
RUNAP_TOLIMA = ROOT / "data" / "interim" / "runap_protected_areas_tolima_exact.geojson"
LINKAGE_CSV = ROOT / "data" / "interim" / "gwis_runap_spatial_linkage_august_2026.csv"
OUTPUT = ROOT / "results" / "exploratory" / "audit_runap_tolima_gwis_linkage.json"
MIN_AREA_HA = 0.01
TO_WGS84 = Transformer.from_crs("EPSG:4686", "EPSG:4326", always_xy=True).transform
TO_UTM18N = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True).transform
TO_WGS84_FROM_UTM = Transformer.from_crs("EPSG:32618", "EPSG:4326", always_xy=True).transform


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def area_ha(geometry) -> float:
    return float(geometry.area / 10_000.0) if not geometry.is_empty else 0.0


def main() -> None:
    for target in (RUNAP_TOLIMA, LINKAGE_CSV, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    boundary_collection = json.loads(BOUNDARY_SOURCE.read_text(encoding="utf-8-sig"))
    tolima_wgs84 = unary_union([make_valid(shape(f["geometry"])) for f in boundary_collection["features"]])
    tolima_utm = transform(TO_UTM18N, tolima_wgs84)

    invalid_before = 0
    repaired = 0
    exact_records = []
    with zipfile.ZipFile(RUNAP_ZIP) as archive:
        prj = archive.read("runap.prj").decode("utf-8", errors="replace")
        with archive.open("runap.shp") as shp, archive.open("runap.shx") as shx, archive.open("runap.dbf") as dbf:
            reader = shapefile.Reader(shp=io.BytesIO(shp.read()), shx=io.BytesIO(shx.read()), dbf=io.BytesIO(dbf.read()))
            fields = [field[0] for field in reader.fields[1:]]
            source_count = len(reader)
            for index, item in enumerate(reader.iterShapeRecords()):
                properties = dict(zip(fields, item.record))
                geometry = shape(item.shape.__geo_interface__)
                if not geometry.is_valid:
                    invalid_before += 1
                    geometry = make_valid(geometry)
                    repaired += 1
                geometry_wgs84 = transform(TO_WGS84, geometry)
                clipped_utm = transform(TO_UTM18N, geometry_wgs84).intersection(tolima_utm)
                if area_ha(clipped_utm) >= MIN_AREA_HA:
                    exact_records.append({
                        "record_index": index,
                        "ap_id": str(properties.get("ap_id", "")),
                        "name": str(properties.get("ap_nombre", "")).strip(),
                        "category": str(properties.get("ap_categor", "")).strip(),
                        "condition": str(properties.get("condicion", "")).strip(),
                        "registration_date": str(properties.get("fecha_regi", "")).strip(),
                        "reported_total_area_ha": properties.get("area_ha_to"),
                        "area_inside_tolima_ha": area_ha(clipped_utm),
                        "geometry_utm": clipped_utm,
                    })

    protected_union_utm = unary_union([record["geometry_utm"] for record in exact_records])
    sum_clipped_area = sum(record["area_inside_tolima_ha"] for record in exact_records)
    union_area = area_ha(protected_union_utm)
    tolima_area = area_ha(tolima_utm)
    category_summary = []
    for category in sorted({record["category"] for record in exact_records}):
        members = [record["geometry_utm"] for record in exact_records if record["category"] == category]
        category_summary.append({
            "category": category,
            "record_count": len(members),
            "union_area_inside_tolima_ha": area_ha(unary_union(members)),
        })

    RUNAP_TOLIMA.parent.mkdir(parents=True, exist_ok=True)
    runap_features = []
    for record in exact_records:
        runap_features.append({
            "type": "Feature",
            "properties": {key: value for key, value in record.items() if key != "geometry_utm"},
            "geometry": mapping(transform(TO_WGS84_FROM_UTM, record["geometry_utm"])),
        })
    RUNAP_TOLIMA.write_text(json.dumps({"type": "FeatureCollection", "features": runap_features}, ensure_ascii=False), encoding="utf-8")

    event_collection = json.loads(EVENT_SOURCE.read_text(encoding="utf-8"))
    rows = []
    for feature in event_collection["features"]:
        event_id = feature["properties"]["fire_id"]
        event_utm = transform(TO_UTM18N, make_valid(shape(feature["geometry"]))).intersection(tolima_utm)
        event_area = area_ha(event_utm)
        overlap_area = area_ha(event_utm.intersection(protected_union_utm))
        overlapping_ids = sorted({record["ap_id"] for record in exact_records if area_ha(event_utm.intersection(record["geometry_utm"])) >= MIN_AREA_HA})
        rows.append({
            "fire_id": event_id,
            "event_area_inside_tolima_ha": round(event_area, 6),
            "protected_area_union_overlap_ha": round(overlap_area, 6),
            "protected_area_union_fraction_of_event": overlap_area / event_area if event_area else None,
            "runap_record_count_intersecting": len(overlapping_ids),
            "runap_ap_ids": "|".join(overlapping_ids),
        })
    rows.sort(key=lambda row: row["fire_id"])
    with LINKAGE_CSV.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    output = {
        "audit_date": "2026-09-06",
        "inference_class": "source and geographic linkage audit",
        "source": {
            "file": str(RUNAP_ZIP.relative_to(ROOT)), "sha256": sha256(RUNAP_ZIP),
            "declared_projection": prj, "source_feature_count": source_count,
            "source_crs_interpreted_as": "EPSG:4686 MAGNA-SIRGAS geographic",
        },
        "geometry_quality": {"invalid_source_geometries": invalid_before, "repaired_with_make_valid": repaired},
        "tolima_exact_intersection": {
            "minimum_retained_area_ha": MIN_AREA_HA,
            "runap_record_count": len(exact_records),
            "unique_ap_id_count": len({record["ap_id"] for record in exact_records}),
            "condition_counts": dict(Counter(record["condition"] for record in exact_records)),
            "category_count": len(category_summary),
            "category_summary": category_summary,
            "sum_record_clipped_area_ha": sum_clipped_area,
            "union_area_inside_tolima_ha": union_area,
            "overlap_removed_by_union_ha": sum_clipped_area - union_area,
            "tolima_area_ha": tolima_area,
            "union_fraction_of_tolima": union_area / tolima_area if tolima_area else None,
        },
        "gwis_linkage": {
            "event_count": len(rows),
            "events_with_at_least_0_01_ha_overlap": sum(row["protected_area_union_overlap_ha"] >= MIN_AREA_HA for row in rows),
            "linkage_type": "exact geographic intersection after clipping both sources to Tolima",
            "file": str(LINKAGE_CSV.relative_to(ROOT)), "sha256": sha256(LINKAGE_CSV),
        },
        "tolima_runap_file": {"file": str(RUNAP_TOLIMA.relative_to(ROOT)), "sha256": sha256(RUNAP_TOLIMA)},
        "interpretation_limit": "RUNAP is a current administrative protected-area layer. Intersection does not establish that an event was independently confirmed, that all overlapped land burned, that protection was effective, or that protected status caused any outcome.",
    }
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "source_feature_count": source_count,
        "tolima_record_count": len(exact_records),
        "unique_ap_id_count": output["tolima_exact_intersection"]["unique_ap_id_count"],
        "union_area_inside_tolima_ha": union_area,
        "events_with_overlap": output["gwis_linkage"]["events_with_at_least_0_01_ha_overlap"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
