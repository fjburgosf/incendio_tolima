"""Refresh the frozen August GWIS cohort beyond the original right boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_august_2026.geojson"
QUERY_OUTPUT = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_aug01_sep06_query_2026-09-06.geojson"
COHORT_OUTPUT = ROOT / "data" / "interim" / "gwis_nrt_burned_area_august_cohort_refreshed_2026-09-06.geojson"
AUDIT_OUTPUT = ROOT / "results" / "exploratory" / "audit_gwis_august_cohort_refresh_2026-09-06.json"
MODULE_AUDIT = ROOT / "results" / "exploratory" / "audit_gwis_nrt_burned_area_tolima_aug01_sep06_query_2026-09-06.json"
RAW_DIR = ROOT / "data" / "raw" / "gwis_nrt_burned_area_aug01_sep06_refreshed_2026-09-06"
PARTIAL_DIR = ROOT / "data" / "raw" / ".gwis_nrt_burned_area_aug01_sep06_refreshed_2026-09-06.partial"
SOURCE_SCRIPT = ROOT / "scripts" / "74_download_audit_gwis_nrt_burned_area_august_2026.py"
EXTENDED_TIME_RANGE = "2026-08-01/2026-09-06"
QUERY_RIGHT_BOUNDARY = datetime(2026, 9, 6, 23, 59, 59)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def load_source_module():
    spec = importlib.util.spec_from_file_location("gwis_download_base", SOURCE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load GWIS source script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_dt(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def main() -> None:
    for target in (QUERY_OUTPUT, COHORT_OUTPUT, AUDIT_OUTPUT, MODULE_AUDIT, RAW_DIR, PARTIAL_DIR):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {target}")

    # Reuse the already audited download and decoding mechanism, changing only
    # destination paths and the time interval. The original cohort is never
    # replaced.
    module = load_source_module()
    module.ACCESS_DATE = "2026-09-06"
    module.TIME_RANGE = EXTENDED_TIME_RANGE
    module.RAW_DIR = RAW_DIR
    module.PARTIAL_DIR = PARTIAL_DIR
    module.INTERIM = QUERY_OUTPUT
    module.OUTPUT = MODULE_AUDIT
    module.main()

    original = json.loads(ORIGINAL.read_text(encoding="utf-8"))
    refreshed = json.loads(QUERY_OUTPUT.read_text(encoding="utf-8"))
    original_by_id = {str(f["properties"]["fire_id"]): f for f in original["features"]}
    refreshed_by_id = {str(f["properties"]["fire_id"]): f for f in refreshed["features"]}
    duplicate_refreshed_ids = len(refreshed_by_id) != len(refreshed["features"])
    missing_ids = sorted(set(original_by_id) - set(refreshed_by_id))
    new_ids = sorted(set(refreshed_by_id) - set(original_by_id))
    comparisons = []
    cohort_features = []
    for fire_id, old_feature in original_by_id.items():
        new_feature = refreshed_by_id.get(fire_id)
        if new_feature is None:
            continue
        old = old_feature["properties"]
        new = new_feature["properties"]
        old_initial = parse_dt(old["initial_datetime_as_exposed"])
        new_initial = parse_dt(new["initial_datetime_as_exposed"])
        old_final = parse_dt(old["final_datetime_as_exposed"])
        new_final = parse_dt(new["final_datetime_as_exposed"])
        comparisons.append({
            "fire_id": fire_id,
            "initial_unchanged": old_initial == new_initial,
            "old_final": old_final.isoformat(sep=" "),
            "new_final": new_final.isoformat(sep=" "),
            "final_not_earlier": new_final >= old_final,
            "final_extended": new_final > old_final,
            "potentially_right_censored_at_refresh_boundary": new_final >= QUERY_RIGHT_BOUNDARY,
            "old_geometry_area_ha": old["geometry_area_ha"],
            "new_geometry_area_ha": new["geometry_area_ha"],
            "old_tolima_area_ha": old["tolima_intersection_area_ha"],
            "new_tolima_area_ha": new["tolima_intersection_area_ha"],
        })
        cohort_features.append(new_feature)

    gate = {
        "original_cohort_count_is_62": len(original_by_id) == 62,
        "all_original_ids_present": not missing_ids,
        "no_duplicate_refreshed_ids": not duplicate_refreshed_ids,
        "all_initial_datetimes_unchanged": all(r["initial_unchanged"] for r in comparisons),
        "no_final_datetime_moved_earlier": all(r["final_not_earlier"] for r in comparisons),
    }
    cohort = {
        "type": "FeatureCollection",
        "name": "Frozen August 2026 GWIS cohort refreshed through 2026-09-06",
        "crs": refreshed.get("crs"),
        "features": cohort_features,
    }
    COHORT_OUTPUT.write_text(json.dumps(cohort, ensure_ascii=False), encoding="utf-8")
    result = {
        "audit_date": "2026-09-06",
        "purpose": "Detect right-boundary truncation in the original August-only GWIS query without redefining the cohort.",
        "acceptance_rule_fixed_before_execution": gate,
        "all_gates_passed": all(gate.values()),
        "original_file": str(ORIGINAL.relative_to(ROOT)),
        "original_sha256": sha256(ORIGINAL),
        "extended_query_time_range": EXTENDED_TIME_RANGE,
        "extended_query_file": str(QUERY_OUTPUT.relative_to(ROOT)),
        "extended_query_sha256": sha256(QUERY_OUTPUT),
        "refreshed_cohort_file": str(COHORT_OUTPUT.relative_to(ROOT)),
        "refreshed_cohort_sha256": sha256(COHORT_OUTPUT),
        "original_cohort_count": len(original_by_id),
        "extended_query_event_count": len(refreshed_by_id),
        "refreshed_cohort_count": len(cohort_features),
        "missing_original_ids": missing_ids,
        "new_ids_not_added_to_frozen_cohort": new_ids,
        "events_with_extended_final_datetime": sum(r["final_extended"] for r in comparisons),
        "events_potentially_right_censored_at_refresh_boundary": sum(r["potentially_right_censored_at_refresh_boundary"] for r in comparisons),
        "comparisons": comparisons,
        "interpretation_rule": "The original 62 identifiers define the cohort. New identifiers from the longer query are not added. A final time at the extended right boundary remains potentially censored.",
    }
    AUDIT_OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "all_gates_passed", "original_cohort_count", "extended_query_event_count",
        "refreshed_cohort_count", "events_with_extended_final_datetime",
        "events_potentially_right_censored_at_refresh_boundary",
    )}, ensure_ascii=False, indent=2))
    if not result["all_gates_passed"]:
        raise RuntimeError("GWIS cohort refresh gate failed")


if __name__ == "__main__":
    main()
