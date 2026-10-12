"""Construct the frozen GWIS event master and thermal anchor table."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[1]
COHORT = ROOT / "data" / "interim" / "gwis_nrt_burned_area_august_cohort_refreshed_2026-09-06.geojson"
BOUNDARY = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
HEAT_DIR = ROOT / "data" / "raw" / "ideam_heat_points_daily"
PLAN = ROOT / "configs" / "analysis_plan_v1.json"
OUTPUT = ROOT / "data" / "interim" / "gwis_event_master_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_gwis_event_master_v1.json"
LOCAL_OFFSET = timedelta(hours=-5)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def parse_local_to_utc(value: str) -> datetime:
    local = datetime.strptime(value, "%Y-%m-%d %H:%M")
    return (local - LOCAL_OFFSET).replace(tzinfo=timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_colombia(value: datetime) -> str:
    return (value + LOCAL_OFFSET).strftime("%Y-%m-%dT%H:%M:00-05:00")


def read_heat_points() -> tuple[list[dict], list[dict]]:
    points, manifest = [], []
    files = sorted(HEAT_DIR.glob("Puntos_de_calor_Colombia_2026-08-*.csv"))
    if len(files) != 31:
        raise RuntimeError(f"Expected 31 heat files, found {len(files)}")
    for path in files:
        count = 0
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle, delimiter=";"):
                try:
                    points.append({
                        "datetime_utc": parse_local_to_utc(row["Fecha (UTC-5)"].strip()),
                        "point": Point(float(row["Lon"].replace(",", ".")), float(row["Lat"].replace(",", "."))),
                        "source": row["Fuente"].strip(),
                    })
                    count += 1
                except (KeyError, ValueError):
                    raise RuntimeError(f"Unexpected usable heat record in {path.name}")
        manifest.append({"file": str(path.relative_to(ROOT)), "sha256": sha256(path), "parsed_rows": count})
    return points, manifest


def main() -> None:
    for target in (OUTPUT, AUDIT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    if plan["version"] != "1.0":
        raise RuntimeError("Unexpected analysis-plan version")
    cohort = json.loads(COHORT.read_text(encoding="utf-8"))
    boundary = json.loads(BOUNDARY.read_text(encoding="utf-8-sig"))
    tolima = unary_union([shape(feature["geometry"]) for feature in boundary["features"]])
    heat_points, heat_manifest = read_heat_points()
    rows = []
    empty_anchor_ids = []
    for feature in cohort["features"]:
        props = feature["properties"]
        geom = shape(feature["geometry"])
        if not geom.is_valid or geom.is_empty:
            raise RuntimeError(f"Invalid GWIS geometry {props['fire_id']}")
        initial = datetime.strptime(props["initial_datetime_as_exposed"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        final = datetime.strptime(props["final_datetime_as_exposed"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if final < initial:
            raise RuntimeError(f"Final time precedes initial time for {props['fire_id']}")
        compatible = [p for p in heat_points if initial <= p["datetime_utc"] <= final and geom.covers(p["point"])]
        tolima_compatible = [p for p in compatible if tolima.covers(p["point"])]
        first_time = min((p["datetime_utc"] for p in compatible), default=None)
        first_points = [p for p in compatible if p["datetime_utc"] == first_time] if first_time else []
        if not first_points:
            empty_anchor_ids.append(str(props["fire_id"]))
            anchor_lon = anchor_lat = None
            anchor_source_count = 0
            anchor_sources = ""
        else:
            anchor_lon = sum(p["point"].x for p in first_points) / len(first_points)
            anchor_lat = sum(p["point"].y for p in first_points) / len(first_points)
            anchor_source_count = len(first_points)
            anchor_sources = "|".join(sorted({p["source"] for p in first_points}))
        initial_local = initial + LOCAL_OFFSET
        fraction_inside = props["tolima_intersection_area_ha"] / props["geometry_area_ha"] if props["geometry_area_ha"] else None
        rows.append({
            "fire_id": str(props["fire_id"]),
            "gwis_initial_datetime_utc": iso_utc(initial),
            "gwis_final_datetime_utc": iso_utc(final),
            "gwis_initial_datetime_colombia": iso_colombia(initial),
            "gwis_final_datetime_colombia": iso_colombia(final),
            "starts_in_august_colombia": initial_local.month == 8,
            "pre_august_start_flag": initial_local.month < 8,
            "gwis_detection_interval_minutes": round((final - initial).total_seconds() / 60.0, 6),
            "zero_detection_interval_flag": final == initial,
            "gwis_reported_area_ha": props["reported_area_ha"],
            "gwis_geometry_area_ha": props["geometry_area_ha"],
            "gwis_tolima_intersection_area_ha": props["tolima_intersection_area_ha"],
            "fraction_geometry_inside_tolima": fraction_inside,
            "first_compatible_heat_datetime_utc": iso_utc(first_time) if first_time else "",
            "anchor_lag_minutes_after_gwis_initial": round((first_time - initial).total_seconds() / 60.0, 6) if first_time else "",
            "anchor_longitude": anchor_lon if anchor_lon is not None else "",
            "anchor_latitude": anchor_lat if anchor_lat is not None else "",
            "anchor_detection_count_at_first_time": anchor_source_count,
            "anchor_sources": anchor_sources,
            "compatible_national_heat_detection_count": len(compatible),
            "compatible_tolima_heat_detection_count": len(tolima_compatible),
            "eligible_primary_before_weather": (
                initial_local.month == 8
                and len(tolima_compatible) > 0
                and props["geometry_area_ha"] > 0
                and bool(first_points)
            ),
            "event_centroid_longitude_for_diagnostics": geom.centroid.x,
            "event_centroid_latitude_for_diagnostics": geom.centroid.y,
        })
    rows.sort(key=lambda row: (row["gwis_initial_datetime_utc"], row["fire_id"]))
    if len(rows) != 62 or len({row["fire_id"] for row in rows}) != 62:
        raise RuntimeError("GWIS event master cardinality gate failed")
    with OUTPUT.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    primary_before_weather = [r for r in rows if r["eligible_primary_before_weather"]]
    audit = {
        "audit_date": "2026-09-06",
        "plan_version": plan["version"],
        "cohort": {"file": str(COHORT.relative_to(ROOT)), "sha256": sha256(COHORT), "event_count": len(rows)},
        "boundary": {"file": str(BOUNDARY.relative_to(ROOT)), "sha256": sha256(BOUNDARY), "crs_note": "Coordinates are treated as longitude latitude WGS84 based on prior source audit. The source GeoJSON does not declare CRS."},
        "heat_files": heat_manifest,
        "anchor_rule": "Earliest compatible national heat-detection timestamp within the frozen GWIS event interval and geometry. Coordinates are the centroid of all compatible points at that timestamp.",
        "empty_anchor_ids": empty_anchor_ids,
        "events_with_exact_first_heat_time_equal_to_gwis_initial": sum(r["anchor_lag_minutes_after_gwis_initial"] == 0 for r in rows),
        "events_with_at_least_one_tolima_compatible_heat_detection": sum(r["compatible_tolima_heat_detection_count"] > 0 for r in rows),
        "primary_cohort_count_before_weather_gate": len(primary_before_weather),
        "primary_excluded_ids_before_weather_gate": [r["fire_id"] for r in rows if not r["eligible_primary_before_weather"]],
        "output": {"file": str(OUTPUT.relative_to(ROOT)), "sha256": sha256(OUTPUT)},
        "interpretation_limit": "Thermal detections define the event anchor and are not an independent exposure or outcome validation. The spatial anchor avoids averaging weather over the final event polygon but does not identify ignition location.",
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: audit[key] for key in ("empty_anchor_ids", "events_with_exact_first_heat_time_equal_to_gwis_initial", "events_with_at_least_one_tolima_compatible_heat_detection", "primary_cohort_count_before_weather_gate", "primary_excluded_ids_before_weather_gate")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
