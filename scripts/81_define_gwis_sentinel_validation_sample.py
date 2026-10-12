"""Predefine a size-stratified spatial sample for Sentinel validation.

Selection uses only GWIS metadata and geometry. It does not inspect Sentinel
spectral values. Events with at least 90 percent of their reported area inside
Tolima are ranked into three equal-count area strata. Three events per stratum
are selected. The first is closest to the stratum median area, except that the
largest already-audited event anchors the upper stratum. Remaining selections
maximize the minimum centroid distance to events already selected in-stratum.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

from shapely.geometry import shape


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_august_2026.geojson"
OUTPUT = ROOT / "results" / "exploratory" / "define_gwis_sentinel_validation_sample.json"
MIN_INSIDE_FRACTION = 0.90
EVENTS_PER_STRATUM = 3
ALREADY_AUDITED_LARGEST = "15606221"


def haversine_km(left: tuple[float, float], right: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, left)
    lon2, lat2 = map(math.radians, right)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(value))


def select_spatially(events: list[dict], anchor_id: str | None = None) -> list[dict]:
    median_area = statistics.median(event["reported_area_ha"] for event in events)
    if anchor_id:
        first = next(event for event in events if event["fire_id"] == anchor_id)
    else:
        first = min(
            events,
            key=lambda event: (abs(event["reported_area_ha"] - median_area), event["fire_id"]),
        )
    selected = [first]
    while len(selected) < EVENTS_PER_STRATUM:
        remaining = [event for event in events if event not in selected]
        next_event = max(
            remaining,
            key=lambda event: (
                min(
                    haversine_km(event["centroid"], chosen["centroid"])
                    for chosen in selected
                ),
                event["reported_area_ha"],
                event["fire_id"],
            ),
        )
        selected.append(next_event)
    return selected


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {OUTPUT}")
    with SOURCE.open(encoding="utf-8") as handle:
        collection = json.load(handle)
    eligible = []
    excluded = []
    for feature in collection["features"]:
        properties = feature["properties"]
        reported = float(properties["reported_area_ha"])
        fraction = float(properties["tolima_intersection_area_ha"]) / reported
        geometry = shape(feature["geometry"])
        record = {
            "fire_id": properties["fire_id"],
            "reported_area_ha": reported,
            "fraction_reported_area_inside_tolima": fraction,
            "initial_datetime": properties["initial_datetime_as_exposed"],
            "final_datetime": properties["final_datetime_as_exposed"],
            "dominant_municipality": properties["municipality_overlaps"][0]["municipality"],
            "centroid": (geometry.centroid.x, geometry.centroid.y),
        }
        if fraction >= MIN_INSIDE_FRACTION:
            eligible.append(record)
        else:
            excluded.append(record)
    eligible.sort(key=lambda event: (event["reported_area_ha"], event["fire_id"]))
    base_size, remainder = divmod(len(eligible), 3)
    strata = []
    start = 0
    for index, label in enumerate(("small", "medium", "large")):
        size = base_size + (1 if index < remainder else 0)
        strata.append((label, eligible[start : start + size]))
        start += size

    selected = []
    stratum_summaries = []
    for label, members in strata:
        chosen = select_spatially(
            members, ALREADY_AUDITED_LARGEST if label == "large" else None
        )
        for order, event in enumerate(chosen, start=1):
            selected.append(
                {
                    **event,
                    "area_stratum": label,
                    "selection_order_within_stratum": order,
                    "already_audited": event["fire_id"] == ALREADY_AUDITED_LARGEST,
                }
            )
        stratum_summaries.append(
            {
                "stratum": label,
                "event_count": len(members),
                "minimum_area_ha": min(event["reported_area_ha"] for event in members),
                "maximum_area_ha": max(event["reported_area_ha"] for event in members),
                "selected_ids": [event["fire_id"] for event in chosen],
            }
        )

    serializable_selected = []
    for event in selected:
        serializable_selected.append({**event, "centroid": list(event["centroid"])})
    result = {
        "definition_date": "2026-09-06",
        "purpose": "measurement validation sample, not a research outcome sample",
        "source_event_count": len(collection["features"]),
        "eligibility_rule": {
            "minimum_fraction_reported_area_inside_tolima": MIN_INSIDE_FRACTION,
            "eligible_event_count": len(eligible),
            "excluded_event_ids": [event["fire_id"] for event in excluded],
            "reason": "reduce ambiguity from cross-department clipping during optical validation",
        },
        "stratification_rule": (
            "Sort eligible events by reported area and divide ranks into three equal-count strata."
        ),
        "within_stratum_selection_rule": (
            "Select the event nearest median area, then maximize minimum centroid distance. "
            "The upper stratum is anchored by the already-audited largest event."
        ),
        "sentinel_rule_to_apply_without_change": {
            "minimum_scl_usable_fraction": 0.80,
            "usable_scl_classes": [4, 5, 6, 7],
            "pre_date": "closest qualifying acquisition before event initial date",
            "post_date": "closest qualifying acquisition after event final date",
            "control_ring_metres": [250, 2000],
            "control_excludes_all_gwis_events": True,
            "validation_direction": "median dNBR inside greater than complete control ring",
            "no_severity_threshold": True,
        },
        "strata": stratum_summaries,
        "selected_event_count": len(serializable_selected),
        "selected_events": serializable_selected,
        "interpretation_limit": (
            "The deterministic sample spans event-size ranks and geography. It is not random, "
            "does not estimate sensitivity for every event, and cannot validate events excluded "
            "for substantial cross-department geometry."
        ),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
