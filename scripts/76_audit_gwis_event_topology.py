"""Audit overlap, timing, and aggregation risks in GWIS Tolima events."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from pyproj import Geod
from shapely.geometry import shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_august_2026.geojson"
BORDER_DIAGNOSIS = ROOT / "results" / "exploratory" / "diagnose_gwis_border_event_15556230.json"
OUTPUT = ROOT / "results" / "exploratory" / "audit_gwis_event_topology_august_2026.json"
GEOD = Geod(ellps="WGS84")


def area_ha(geometry) -> float:
    if geometry.is_empty:
        return 0.0
    return abs(GEOD.geometry_area_perimeter(geometry)[0]) / 10_000.0


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {OUTPUT}")
    with SOURCE.open(encoding="utf-8") as handle:
        collection = json.load(handle)
    with BORDER_DIAGNOSIS.open(encoding="utf-8") as handle:
        border_diagnosis = json.load(handle)

    events = []
    for feature in collection["features"]:
        properties = feature["properties"]
        initial_utc = datetime.strptime(
            properties["initial_datetime_as_exposed"], "%Y-%m-%d %H:%M:%S"
        )
        final_utc = datetime.strptime(
            properties["final_datetime_as_exposed"], "%Y-%m-%d %H:%M:%S"
        )
        events.append(
            {
                "fire_id": properties["fire_id"],
                "initial_utc": initial_utc,
                "final_utc": final_utc,
                "duration_hours": (final_utc - initial_utc).total_seconds() / 3600,
                "reported_area_ha": properties["reported_area_ha"],
                "tolima_intersection_area_ha": properties["tolima_intersection_area_ha"],
                "municipalities": [
                    overlap["municipality"] for overlap in properties["municipality_overlaps"]
                ],
                "point_count": properties["tolima_firms_points_within_event_time_and_geometry"],
                "geometry": shape(feature["geometry"]),
            }
        )

    overlaps = []
    for index, left in enumerate(events):
        for right in events[index + 1 :]:
            intersection = left["geometry"].intersection(right["geometry"])
            intersection_area = area_ha(intersection)
            if intersection_area <= 0.01:
                continue
            temporal_overlap_hours = max(
                0.0,
                (
                    min(left["final_utc"], right["final_utc"])
                    - max(left["initial_utc"], right["initial_utc"])
                ).total_seconds()
                / 3600,
            )
            overlaps.append(
                {
                    "left_fire_id": left["fire_id"],
                    "right_fire_id": right["fire_id"],
                    "intersection_area_ha": round(intersection_area, 3),
                    "temporal_overlap_hours": round(temporal_overlap_hours, 3),
                }
            )
    overlaps.sort(key=lambda item: item["intersection_area_ha"], reverse=True)

    geometry_area_sum = sum(area_ha(event["geometry"]) for event in events)
    union_area = area_ha(unary_union([event["geometry"] for event in events]))
    durations = [event["duration_hours"] for event in events]
    reported_areas = [event["reported_area_ha"] for event in events]
    initial_utc_counts = Counter(event["initial_utc"].strftime("%Y-%m-%d") for event in events)
    initial_local_counts = Counter(
        (event["initial_utc"] - timedelta(hours=5)).strftime("%Y-%m-%d") for event in events
    )
    zero_tolima_point_events = [event for event in events if event["point_count"] == 0]
    outside_fraction_events = []
    for event in events:
        reported = event["reported_area_ha"]
        inside = event["tolima_intersection_area_ha"]
        if reported:
            outside_fraction_events.append(
                {
                    "fire_id": event["fire_id"],
                    "fraction_of_reported_area_inside_tolima": inside / reported,
                    "reported_area_ha": reported,
                    "tolima_intersection_area_ha": inside,
                }
            )
    outside_fraction_events.sort(key=lambda item: item["fraction_of_reported_area_inside_tolima"])

    result = {
        "audit_date": "2026-09-06",
        "event_count": len(events),
        "temporal": {
            "initial_date_utc_counts": dict(sorted(initial_utc_counts.items())),
            "initial_date_colombia_local_counts": dict(sorted(initial_local_counts.items())),
            "minimum_duration_hours": min(durations),
            "median_duration_hours": statistics.median(durations),
            "maximum_duration_hours": max(durations),
            "events_finalizing_after_august_31_utc": sum(
                event["final_utc"] >= datetime(2026, 9, 1) for event in events
            ),
        },
        "area": {
            "minimum_reported_area_ha": min(reported_areas),
            "median_reported_area_ha": statistics.median(reported_areas),
            "maximum_reported_area_ha": max(reported_areas),
            "sum_event_geometry_area_ha": round(geometry_area_sum, 3),
            "union_event_geometry_area_ha": round(union_area, 3),
            "sum_minus_union_area_ha": round(geometry_area_sum - union_area, 3),
            "pair_count_with_spatial_overlap_gt_0_01_ha": len(overlaps),
            "pair_count_with_spatial_and_temporal_overlap": sum(
                overlap["temporal_overlap_hours"] > 0 for overlap in overlaps
            ),
            "largest_pairwise_overlaps": overlaps[:20],
            "five_smallest_fractions_inside_tolima": outside_fraction_events[:5],
        },
        "source_detection_support": {
            "events_with_tolima_points": len(events) - len(zero_tolima_point_events),
            "events_without_tolima_points": [event["fire_id"] for event in zero_tolima_point_events],
            "border_event_national_point_count": border_diagnosis[
                "national_points_inside_event_time_and_geometry"
            ],
            "all_events_have_tolima_or_national_point_support": (
                len(zero_tolima_point_events) == 1
                and zero_tolima_point_events[0]["fire_id"] == border_diagnosis["fire_id"]
                and border_diagnosis["national_points_inside_event_time_and_geometry"] > 0
            ),
        },
        "largest_events": [
            {
                key: value
                for key, value in event.items()
                if key
                in {
                    "fire_id",
                    "duration_hours",
                    "reported_area_ha",
                    "tolima_intersection_area_ha",
                    "municipalities",
                    "point_count",
                }
            }
            for event in sorted(events, key=lambda item: item["reported_area_ha"], reverse=True)[:15]
        ],
        "aggregation_rule": (
            "Do not sum whole-event reported area for a Tolima total when an event crosses the "
            "department boundary. Use the Tolima-clipped geometry. Also report union area or "
            "event-specific area because distinct event polygons can overlap in space."
        ),
        "interpretive_limit": (
            "The topology audit checks internal consistency only. It cannot establish accuracy "
            "because event polygons are derived from the same FIRMS detections used for support."
        ),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
