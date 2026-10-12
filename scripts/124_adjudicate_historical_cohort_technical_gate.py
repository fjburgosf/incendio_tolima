"""Adjudicate pre-specified technical gates without constructing a historical model."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCESS_DATE = "2026-09-07"
CONFIG = ROOT / "configs" / "historical_cohort_comparability_gate_v1.json"
GWIS_SCHEMA = ROOT / "results" / "exploratory" / f"audit_gwis_historical_schema_geometry_{ACCESS_DATE}_v2.json"
GWIS_EVENTS = ROOT / "data" / "interim" / f"gwis_historical_event_schema_summary_{ACCESS_DATE}_v2.csv"
GWIS_FOLLOW_UP = ROOT / "results" / "exploratory" / f"audit_gwis_historical_temporal_follow_up_{ACCESS_DATE}.json"
ERA5_SNAPSHOT = ROOT / "results" / "exploratory" / f"audit_recovered_era5_land_historical_weather_access_{ACCESS_DATE}.json"
NASA_PIXEL = ROOT / "results" / "exploratory" / f"audit_nasa_burned_area_hdf_georeference_pixel_qc_{ACCESS_DATE}_v2.json"
NASA_CONCORDANCE = ROOT / "results" / "exploratory" / f"nasa_burned_area_cross_product_concordance_{ACCESS_DATE}.json"
OUTPUT = ROOT / "results" / "exploratory" / f"adjudicate_historical_cohort_technical_gate_{ACCESS_DATE}.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def condition(name: str, passed: bool, evidence: object, limit: str) -> dict[str, object]:
    return {"name": name, "passed": passed, "evidence": evidence, "limit": limit}


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {OUTPUT}")
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    schema = json.loads(GWIS_SCHEMA.read_text(encoding="utf-8"))
    follow_up = json.loads(GWIS_FOLLOW_UP.read_text(encoding="utf-8"))
    era5 = json.loads(ERA5_SNAPSHOT.read_text(encoding="utf-8"))
    nasa_pixel = json.loads(NASA_PIXEL.read_text(encoding="utf-8"))
    nasa_concordance = json.loads(NASA_CONCORDANCE.read_text(encoding="utf-8"))
    event_rows = list(csv.DictReader(GWIS_EVENTS.open(encoding="utf-8")))
    required = config["required_source_checks"]
    seasons = [2018, 2020, 2022, 2024, 2025]
    required_fields = required["gwis_event_schema"]["required_fields"]
    observed_schemas = schema["observed_property_names_by_season"]
    schema_pass = all(observed_schemas[str(year)] == required_fields for year in config["candidate_seasons"])
    property_conflicts = {year: schema["season_audits"][str(year)]["property_conflict_count"] for year in config["candidate_seasons"]}
    max_area_difference = max(schema["season_audits"][str(year)]["relative_area_difference_max"] for year in config["candidate_seasons"])
    positive_area = all(float(row["reported_area_ha"]) > 0 for row in event_rows)
    historical_rows = [row for row in event_rows if int(row["season"]) in seasons]
    candidate_rows = [
        row for row in historical_rows
        if row["initialdate"][:10] >= f"{row['season']}-08-01"
        and row["initialdate"][:10] <= f"{row['season']}-08-31"
        and row["finaldate"][:10] <= f"{row['season']}-09-15"
    ]
    follow_up_pass = all(
        not follow_up["seasonal_summary"][str(year)]["baseline_events_missing_from_follow_up"]
        and follow_up["seasonal_summary"][str(year)]["baseline_initialdate_changed_count"] == 0
        and follow_up["seasonal_summary"][str(year)]["baseline_finaldate_changed_count"] == 0
        and follow_up["seasonal_summary"][str(year)]["baseline_follow_up_final_after_september_15_count"] == 0
        for year in seasons
    )
    expected_short_names = {"2t", "2d", "10u", "10v"}
    era5_snapshot_pass = (
        era5["record_count"] == era5["expected_record_count"]
        and not era5["missing_year_variable_pairs"]
        and not era5["unexpected_year_variable_pairs"]
        and {record["short_name"] for record in era5["records"]} == expected_short_names
    )
    conditions = [
        condition(
            "GWIS fields are exactly the required fields in every candidate season",
            schema_pass,
            observed_schemas,
            "Equal field names do not prove constant detection sensitivity or processing across years.",
        ),
        condition(
            "GWIS tiled events have no within-event property conflict",
            all(value == 0 for value in property_conflicts.values()),
            property_conflicts,
            "This applies to reconstructed events only, not the full regional product population.",
        ),
        condition(
            "GWIS geometry versus reported area difference stays within the pre-set tolerance",
            max_area_difference <= required["gwis_event_schema"]["geometry_area_relative_difference_maximum"],
            {
                "maximum_observed": max_area_difference,
                "maximum_allowed": required["gwis_event_schema"]["geometry_area_relative_difference_maximum"],
            },
            "The test checks internal geometric coherence, not the true burned area.",
        ),
        condition(
            "Every retained GWIS event has positive reported area",
            positive_area,
            {"retained_event_rows": len(event_rows)},
            "Reported area remains an operational product attribute.",
        ),
        condition(
            "Completed historical seasons pass the common September 15 follow-up audit",
            follow_up_pass,
            follow_up["seasonal_summary"],
            "This addresses request-window stability for baseline August events only.",
        ),
        condition(
            "ERA5 Land has the four historical variables on a common audited snapshot",
            era5_snapshot_pass,
            {
                "records": era5["record_count"],
                "short_names": sorted({record["short_name"] for record in era5["records"]}),
            },
            "A common snapshot proves source access and schema. It is not enough to construct event windows.",
        ),
        condition(
            "NASA historical pixel-georeference and quality audit passes",
            nasa_pixel["acceptance_result"] == "pass",
            {"records": len(nasa_pixel["records"]), "audit": str(NASA_PIXEL.relative_to(ROOT))},
            "NASA does not cover August 2026 in the current catalogue audit.",
        ),
        condition(
            "NASA cross-product concordance is computed under a pre-specified rule",
            nasa_concordance["acceptance_result"] == "pass",
            {"years": [record["year"] for record in nasa_concordance["annual_records"]]},
            "Concordance is descriptive and cannot validate GWIS or identify a true burned area.",
        ),
    ]
    technical_gate_pass = all(bool(item["passed"]) for item in conditions)
    candidate_counts = {year: sum(int(row["season"]) == year for row in candidate_rows) for year in seasons}
    output = {
        "audit_date": ACCESS_DATE,
        "inference_class": "technical cohort-eligibility adjudication",
        "source_hashes": {str(path.relative_to(ROOT)): sha256(path) for path in [CONFIG, GWIS_SCHEMA, GWIS_EVENTS, GWIS_FOLLOW_UP, ERA5_SNAPSHOT, NASA_PIXEL, NASA_CONCORDANCE]},
        "conditions": conditions,
        "technical_gate_pass": technical_gate_pass,
        "proposed_historical_event_definition_for_human_review_only": {
            "seasons": seasons,
            "geography": config["primary_geography"],
            "event_rule": "GWIS event geometry intersects Tolima, operational initial timestamp is in August, and audited follow-up operational final timestamp is no later than September 15.",
            "candidate_event_counts": candidate_counts,
            "candidate_total": len(candidate_rows),
            "excluded_2026_reason": "The September 15 follow-up requirement was not auditable on 7 September 2026.",
        },
        "remaining_blockers_before_any_historical_associational_or_comparative_model": [
            "Historical ERA5 Land is only audited as a one-time snapshot. Full event-window meteorological inputs have not been downloaded or quality-audited.",
            "The technical checks do not demonstrate unchanged GWIS processing, equal satellite observation opportunity, event completeness, or environmental comparability between seasons.",
            "NASA MCD64A1 and VNP64A1 disagree substantially and cannot be combined or treated as validation without a new documented design.",
            "No neighbouring department has passed an equivalent source and comparability audit. Regional pooling remains prohibited.",
            "The investigator must review and authorise any final historical research question and analysis plan before a model is constructed."
        ],
        "decision": (
            "Technical data-source gates pass for a candidate historical Tolima event cohort in 2018, 2020, 2022, 2024 and 2025. "
            "This is not an analytical-comparability approval and does not authorise modelling."
        ),
    }
    OUTPUT.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "technical_gate_pass": technical_gate_pass, "candidate_event_counts": candidate_counts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
