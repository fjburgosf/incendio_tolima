"""Construct nonduplicated GWIS NRT area summaries clipped to Tolima."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

from pyproj import Geod
from shapely.geometry import GeometryCollection, shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[1]
EVENT_SOURCE = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_august_2026.geojson"
BOUNDARY_SOURCE = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
CSV_OUTPUT = ROOT / "data" / "interim" / "gwis_nrt_burned_area_municipality_summary_august_2026.csv"
JSON_OUTPUT = ROOT / "results" / "exploratory" / "gwis_nrt_burned_area_tolima_clipped_summary_august_2026.json"
GEOD = Geod(ellps="WGS84")


def area_ha(geometry) -> float:
    if geometry.is_empty:
        return 0.0
    return abs(GEOD.geometry_area_perimeter(geometry)[0]) / 10_000.0


def main() -> None:
    for target in (CSV_OUTPUT, JSON_OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    with EVENT_SOURCE.open(encoding="utf-8") as handle:
        event_collection = json.load(handle)
    with BOUNDARY_SOURCE.open(encoding="utf-8-sig") as handle:
        boundary_collection = json.load(handle)

    events = []
    for feature in event_collection["features"]:
        properties = feature["properties"]
        initial_utc = datetime.strptime(
            properties["initial_datetime_as_exposed"], "%Y-%m-%d %H:%M:%S"
        )
        events.append(
            {
                "fire_id": properties["fire_id"],
                "initial_local": initial_utc - timedelta(hours=5),
                "geometry": shape(feature["geometry"]),
            }
        )
    municipalities = [
        (feature["properties"]["mun_nombre"], shape(feature["geometry"]))
        for feature in boundary_collection["features"]
    ]
    tolima_geometry = unary_union([geometry for _, geometry in municipalities])

    event_clips = [event["geometry"].intersection(tolima_geometry) for event in events]
    event_clips = [geometry for geometry in event_clips if not geometry.is_empty]
    tolima_union = unary_union(event_clips)
    tolima_area = area_ha(tolima_geometry)
    sum_event_clips = sum(area_ha(geometry) for geometry in event_clips)
    union_area = area_ha(tolima_union)

    rows = []
    for municipality, municipal_geometry in municipalities:
        intersections = []
        fire_ids = []
        for event in events:
            intersection = event["geometry"].intersection(municipal_geometry)
            if area_ha(intersection) > 0.01:
                intersections.append(intersection)
                fire_ids.append(event["fire_id"])
        union = unary_union(intersections) if intersections else GeometryCollection()
        municipal_area = area_ha(municipal_geometry)
        estimated_area = area_ha(union)
        rows.append(
            {
                "municipality": municipality,
                "municipality_area_ha": round(municipal_area, 3),
                "gwis_event_count_intersecting": len(fire_ids),
                "gwis_event_ids": "|".join(sorted(fire_ids)),
                "gwis_union_area_within_municipality_ha": round(estimated_area, 3),
                "gwis_union_fraction_of_municipality": (
                    estimated_area / municipal_area if municipal_area else None
                ),
            }
        )
    rows.sort(key=lambda row: row["municipality"])
    CSV_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with CSV_OUTPUT.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    local_august_events = [
        event
        for event in events
        if datetime(2026, 8, 1) <= event["initial_local"] < datetime(2026, 9, 1)
    ]
    local_pre_august_events = [
        event for event in events if event["initial_local"] < datetime(2026, 8, 1)
    ]
    ranked = sorted(
        rows, key=lambda row: row["gwis_union_area_within_municipality_ha"], reverse=True
    )
    result = {
        "audit_date": "2026-09-06",
        "scope": "GWIS NRT event polygons returned for 2026-08-01/2026-08-31 and intersecting Tolima",
        "event_population": {
            "all_interval_query_events_intersecting_tolima": len(events),
            "events_starting_during_august_in_colombia_local_time": len(local_august_events),
            "events_starting_before_august_local_but_returned_by_interval_query": [
                event["fire_id"] for event in local_pre_august_events
            ],
            "rule": (
                "Retain all interval-query events for coverage. Flag pre-August starts separately. "
                "Do not silently redefine August events after inspecting the distribution."
            ),
        },
        "nonduplicated_area": {
            "tolima_boundary_area_ha_geodesic": round(tolima_area, 3),
            "sum_event_areas_after_tolima_clip_ha": round(sum_event_clips, 3),
            "union_area_after_tolima_clip_ha": round(union_area, 3),
            "spatial_overlap_removed_by_union_ha": round(sum_event_clips - union_area, 3),
            "union_fraction_of_tolima": union_area / tolima_area,
            "warning": (
                "These are GWIS NRT estimates derived from FIRMS thermal anomalies. They are not "
                "independently observed burned area and must not be presented as official damage."
            ),
        },
        "municipalities": {
            "municipality_count_in_boundary_file": len(rows),
            "municipalities_with_gwis_event_intersection": sum(
                row["gwis_event_count_intersecting"] > 0 for row in rows
            ),
            "top_ten_by_nonduplicated_gwis_area": [
                {
                    "municipality": row["municipality"],
                    "event_count": row["gwis_event_count_intersecting"],
                    "union_area_ha": row["gwis_union_area_within_municipality_ha"],
                    "union_fraction_of_municipality": row[
                        "gwis_union_fraction_of_municipality"
                    ],
                }
                for row in ranked[:10]
            ],
        },
        "outputs": {
            "municipality_csv": str(CSV_OUTPUT.relative_to(ROOT)),
            "event_geojson": str(EVENT_SOURCE.relative_to(ROOT)),
        },
    }
    JSON_OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
