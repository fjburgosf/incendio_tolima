"""Diagnose the GWIS event intersecting Tolima without a Tolima FIRMS point."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

from shapely.geometry import Point, shape


ROOT = Path(__file__).resolve().parents[1]
EVENT_SOURCE = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_august_2026.geojson"
NATIONAL_SOURCE = ROOT / "data" / "raw" / "ideam_heat_points_daily" / "Puntos_de_calor_Colombia_2026-08-02.csv"
OUTPUT = ROOT / "results" / "exploratory" / "diagnose_gwis_border_event_15556230.json"
FIRE_ID = "15556230"


def parse_decimal(value: str) -> float:
    return float(value.replace(",", "."))


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {OUTPUT}")
    with EVENT_SOURCE.open(encoding="utf-8") as handle:
        collection = json.load(handle)
    event_feature = next(
        feature for feature in collection["features"] if feature["properties"]["fire_id"] == FIRE_ID
    )
    event_geometry = shape(event_feature["geometry"])
    properties = event_feature["properties"]
    initial = datetime.strptime(properties["initial_datetime_as_exposed"], "%Y-%m-%d %H:%M:%S")
    final = datetime.strptime(properties["final_datetime_as_exposed"], "%Y-%m-%d %H:%M:%S")

    matching_points = []
    with NATIONAL_SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            local_datetime = datetime.strptime(row["Fecha (UTC-5)"], "%Y-%m-%d %H:%M")
            utc_datetime = local_datetime + timedelta(hours=5)
            point = Point(parse_decimal(row["Lon"]), parse_decimal(row["Lat"]))
            if initial <= utc_datetime <= final and event_geometry.covers(point):
                matching_points.append(
                    {
                        "datetime_local_utc_minus_5": row["Fecha (UTC-5)"],
                        "datetime_utc_transformed": utc_datetime.strftime("%Y-%m-%d %H:%M"),
                        "latitude": point.y,
                        "longitude": point.x,
                        "source": row["Fuente"],
                        "confidence": row["Confianza"],
                    }
                )

    result = {
        "audit_date": "2026-09-06",
        "fire_id": FIRE_ID,
        "event_reported_area_ha": properties["reported_area_ha"],
        "event_tolima_intersection_area_ha": properties["tolima_intersection_area_ha"],
        "national_points_inside_event_time_and_geometry": len(matching_points),
        "national_points": matching_points,
        "diagnosis": (
            "The event is supported by national IDEAM/FIRMS heat points, while none belongs to "
            "the Tolima-only point subset. Its small polygon overlap with Melgar is therefore a "
            "cross-boundary intersection, not evidence that GWIS created an event without source detections."
        ),
        "remaining_limitation": (
            "The national CSV has no administrative field. The outside-Tolima status is established "
            "by the prior point-in-polygon audit against the Tolima municipal union."
        ),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
