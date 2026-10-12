"""Join frozen event outcomes, elevation, and ERA5 features into one analysis table."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "analysis_plan_v1.json"
AMENDMENT = ROOT / "configs" / "analysis_plan_amendment_2026-09-06_mapbiomas.json"
MASTER = ROOT / "data" / "interim" / "gwis_event_master_v1.csv"
DEM = ROOT / "data" / "interim" / "gwis_event_anchor_dem_glo90_v1.csv"
WEATHER = ROOT / "data" / "interim" / "gwis_event_era5_features_v1.csv"
OUTPUT = ROOT / "data" / "processed" / "gwis_event_analysis_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_gwis_event_analysis_table_v1.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def read_by_fire_id(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    identifiers = [row["fire_id"] for row in rows]
    if len(identifiers) != len(set(identifiers)):
        raise RuntimeError(f"Duplicate fire_id in {path.name}")
    return {row["fire_id"]: row for row in rows}


def numeric(value: str) -> float:
    return float(value)


def main() -> None:
    for target in (OUTPUT, AUDIT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    amendment = json.loads(AMENDMENT.read_text(encoding="utf-8"))
    if plan["version"] != "1.0" or amendment["amendment_version"] != "1.1":
        raise RuntimeError("Unexpected plan or amendment version")
    master = read_by_fire_id(MASTER)
    dem = read_by_fire_id(DEM)
    weather = read_by_fire_id(WEATHER)
    if set(master) != set(dem) or set(master) != set(weather) or len(master) != 62:
        raise RuntimeError("Event linkage cardinality gate failed")
    rows = []
    missing_primary_fields = []
    for fire_id, event in master.items():
        elevation = dem[fire_id]
        met = weather[fire_id]
        geometry_area = numeric(event["gwis_geometry_area_ha"])
        interval_hours = numeric(event["gwis_detection_interval_minutes"]) / 60.0
        if geometry_area <= 0 or interval_hours < 0:
            raise RuntimeError(f"Invalid outcome component for {fire_id}")
        primary = event["eligible_primary_before_weather"].strip().lower() == "true" and met["primary_weather_complete"].strip().lower() == "true"
        row = {
            "fire_id": fire_id,
            "primary_cohort": primary,
            "pre_august_start_flag": event["pre_august_start_flag"],
            "gwis_initial_datetime_utc": event["gwis_initial_datetime_utc"],
            "gwis_initial_datetime_colombia": event["gwis_initial_datetime_colombia"],
            "gwis_final_datetime_utc": event["gwis_final_datetime_utc"],
            "gwis_geometry_area_ha": geometry_area,
            "gwis_reported_area_ha": numeric(event["gwis_reported_area_ha"]),
            "gwis_tolima_intersection_area_ha": numeric(event["gwis_tolima_intersection_area_ha"]),
            "fraction_geometry_inside_tolima": numeric(event["fraction_geometry_inside_tolima"]),
            "gwis_detection_interval_hours": interval_hours,
            "log_geometry_area_ha": math.log(geometry_area),
            "log1p_detection_interval_hours": math.log1p(interval_hours),
            "anchor_longitude": numeric(event["anchor_longitude"]),
            "anchor_latitude": numeric(event["anchor_latitude"]),
            "anchor_detection_count_at_first_time": int(event["anchor_detection_count_at_first_time"]),
            "compatible_national_heat_detection_count": int(event["compatible_national_heat_detection_count"]),
            "compatible_tolima_heat_detection_count": int(event["compatible_tolima_heat_detection_count"]),
            "anchor_elevation_m": numeric(elevation["dem_elevation_m"]),
            "anchor_to_dem_pixel_center_distance_m": numeric(elevation["anchor_to_dem_pixel_center_distance_m"]),
            "vpd_mean_first_24h_kpa": numeric(met["vpd_mean_first_24h_kpa"]),
            "vpd_p95_first_24h_kpa": numeric(met["vpd_p95_first_24h_kpa"]),
            "wind_mean_first_24h_ms": numeric(met["wind_mean_first_24h_ms"]),
            "wind_p95_first_24h_ms": numeric(met["wind_p95_first_24h_ms"]),
            "temperature_mean_first_24h_c": numeric(met["temperature_mean_first_24h_c"]),
            "temperature_max_first_24h_c": numeric(met["temperature_max_first_24h_c"]),
            "precipitation_first_24h_mm": numeric(met["precipitation_first_24h_mm"]),
            "precipitation_previous_7_complete_local_days_mm": numeric(met["precipitation_previous_7_complete_local_days_mm"]),
            "maximum_consecutive_dry_days_previous_30_complete_local_days": int(met["maximum_consecutive_dry_days_previous_30_complete_local_days"]),
        }
        required = ("log_geometry_area_ha", "log1p_detection_interval_hours", "anchor_elevation_m", "vpd_mean_first_24h_kpa", "wind_mean_first_24h_ms")
        missing = [name for name in required if not math.isfinite(float(row[name]))]
        if primary and missing:
            missing_primary_fields.append({"fire_id": fire_id, "fields": missing})
        rows.append(row)
    if missing_primary_fields:
        raise RuntimeError(f"Missing primary analytic values: {missing_primary_fields}")
    rows.sort(key=lambda row: (row["gwis_initial_datetime_utc"], row["fire_id"]))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    primary = [row for row in rows if row["primary_cohort"]]
    excluded = [row["fire_id"] for row in rows if not row["primary_cohort"]]
    audit = {
        "plan": {"file": str(PLAN.relative_to(ROOT)), "sha256": sha256(PLAN), "version": plan["version"]},
        "amendment": {"file": str(AMENDMENT.relative_to(ROOT)), "sha256": sha256(AMENDMENT), "version": amendment["amendment_version"]},
        "inputs": {
            "event_master": {"file": str(MASTER.relative_to(ROOT)), "sha256": sha256(MASTER), "row_count": len(master)},
            "dem": {"file": str(DEM.relative_to(ROOT)), "sha256": sha256(DEM), "row_count": len(dem)},
            "weather": {"file": str(WEATHER.relative_to(ROOT)), "sha256": sha256(WEATHER), "row_count": len(weather)},
        },
        "row_count": len(rows),
        "primary_cohort_count": len(primary),
        "nonprimary_event_ids": excluded,
        "primary_required_fields": ["log_geometry_area_ha", "log1p_detection_interval_hours", "anchor_elevation_m", "vpd_mean_first_24h_kpa", "wind_mean_first_24h_ms"],
        "primary_missing_field_records": missing_primary_fields,
        "output": {"file": str(OUTPUT.relative_to(ROOT)), "sha256": sha256(OUTPUT)},
        "interpretation_limit": "This table is the preregistered descriptive and associational analysis input. It contains operational GWIS outcomes and reanalysis covariates. Its construction does not produce effect estimates or validate burned area, duration, cause, or ignition.",
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"row_count": len(rows), "primary_cohort_count": len(primary), "nonprimary_event_ids": excluded, "primary_missing_field_records": missing_primary_fields}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
