"""Replace blocked precipitation features with audited within-cycle ERA5 increments."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "precipitation_reconstruction_and_exploratory_analysis_plan_v2.json"
MASTER = ROOT / "data" / "interim" / "gwis_event_master_v1.csv"
WEIGHTS = ROOT / "data" / "interim" / "gwis_event_era5_anchor_weights_v1.csv"
INCREMENTS = ROOT / "data" / "interim" / "era5_land_hourly_precipitation_increment_v3.npz"
ANALYSIS_V1 = ROOT / "data" / "processed" / "gwis_event_analysis_v1.csv"
FEATURE_OUT = ROOT / "data" / "interim" / "gwis_event_precipitation_features_v3.csv"
ANALYSIS_OUT = ROOT / "data" / "processed" / "gwis_event_analysis_v2.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_gwis_event_precipitation_rebuild_v3.json"
LOCAL = timezone(timedelta(hours=-5))
PRECIP_COLUMNS = [
    "precipitation_first_24h_mm",
    "precipitation_previous_7_complete_local_days_mm",
    "maximum_consecutive_dry_days_previous_30_complete_local_days",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def consecutive_dry_days(values: list[float], threshold_mm: float = 1.0) -> int:
    current = 0
    longest = 0
    for value in values:
        if value < threshold_mm:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    for path in (FEATURE_OUT, ANALYSIS_OUT, AUDIT):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {path}")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    with MASTER.open(encoding="utf-8-sig", newline="") as handle:
        events = list(csv.DictReader(handle))
    with WEIGHTS.open(encoding="utf-8-sig", newline="") as handle:
        weight_rows = list(csv.DictReader(handle))
    weights_by_event: dict[str, list[dict[str, str]]] = {}
    for row in weight_rows:
        weights_by_event.setdefault(row["fire_id"], []).append(row)
    if len(events) != 62 or set(weights_by_event) != {row["fire_id"] for row in events}:
        raise RuntimeError("Event master and pre-audited anchor weights do not align")
    archive = np.load(INCREMENTS, allow_pickle=False)
    times = [parse_utc(str(value)) for value in archive["validity_utc"]]
    increments_mm = np.asarray(archive["tp_increment_m"], dtype=float) * 1000.0
    if increments_mm.shape != (len(times), 989) or not np.isfinite(increments_mm).all() or np.any(increments_mm < 0):
        raise RuntimeError("Invalid audited precipitation increment archive")
    time_index = {time: position for position, time in enumerate(times)}
    feature_rows: list[dict[str, object]] = []
    for event in events:
        fire_id = event["fire_id"]
        rows = sorted(weights_by_event[fire_id], key=lambda row: int(row["corner_rank"]))
        point_indices = np.asarray([int(row["grid_point_index"]) for row in rows], dtype=int)
        coefficients = np.asarray([float(row["weight"]) for row in rows], dtype=float)
        if not np.isclose(coefficients.sum(), 1.0):
            raise RuntimeError(f"Anchor weights do not sum to one for {fire_id}")
        initial_utc = parse_utc(event["gwis_initial_datetime_utc"])
        first_window = [time for time in times if initial_utc < time <= initial_utc + timedelta(hours=24)]
        if len(first_window) != 24:
            raise RuntimeError(f"Incomplete first-24-hour rainfall window for {fire_id}")
        first_values = increments_mm[[time_index[time] for time in first_window]][:, point_indices] @ coefficients
        initial_local = datetime.fromisoformat(event["gwis_initial_datetime_colombia"])
        daily_totals: dict[str, float] = {}
        for offset in range(1, 31):
            local_date = (initial_local - timedelta(days=offset)).date()
            local_start = datetime.combine(local_date, datetime.min.time(), tzinfo=LOCAL)
            local_end = local_start + timedelta(days=1)
            selected = [time_index[time] for time in times if local_start.astimezone(timezone.utc) < time <= local_end.astimezone(timezone.utc)]
            if len(selected) != 24:
                raise RuntimeError(f"Incomplete antecedent rainfall day for {fire_id} {local_date.isoformat()}")
            daily_totals[local_date.isoformat()] = float(np.sum(increments_mm[selected][:, point_indices] @ coefficients))
        prior_dates = [(initial_local - timedelta(days=offset)).date().isoformat() for offset in range(1, 31)]
        prior_7 = prior_dates[:7]
        feature_rows.append(
            {
                "fire_id": fire_id,
                "precipitation_first_24h_mm": float(np.sum(first_values)),
                "precipitation_previous_7_complete_local_days_mm": float(sum(daily_totals[date] for date in prior_7)),
                "maximum_consecutive_dry_days_previous_30_complete_local_days": consecutive_dry_days([daily_totals[date] for date in reversed(prior_dates)]),
                "first_window_hour_count": len(first_window),
                "antecedent_day_count": len(daily_totals),
            }
        )
    feature_by_id = {row["fire_id"]: row for row in feature_rows}
    with ANALYSIS_V1.open(encoding="utf-8-sig", newline="") as handle:
        analysis_v1 = list(csv.DictReader(handle))
    if set(feature_by_id) != {row["fire_id"] for row in analysis_v1}:
        raise RuntimeError("Feature and v1 analysis identifiers do not align")
    analysis_v2 = []
    differences = {column: [] for column in PRECIP_COLUMNS}
    for row in analysis_v1:
        item = dict(row)
        features = feature_by_id[row["fire_id"]]
        for column in PRECIP_COLUMNS:
            differences[column].append(float(features[column]) - float(item[column]))
            item[column] = features[column]
        analysis_v2.append(item)
    write_csv(FEATURE_OUT, sorted(feature_rows, key=lambda row: row["fire_id"]), list(feature_rows[0]))
    write_csv(ANALYSIS_OUT, analysis_v2, list(analysis_v1[0]))
    audit = {
        "plan": {"file": str(PLAN.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(PLAN)},
        "inputs": {
            "event_master": {"file": str(MASTER.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(MASTER)},
            "anchor_weights": {"file": str(WEIGHTS.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(WEIGHTS)},
            "precipitation_increments": {"file": str(INCREMENTS.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(INCREMENTS)},
            "analysis_v1": {"file": str(ANALYSIS_V1.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(ANALYSIS_V1)},
        },
        "event_count": len(feature_rows),
        "primary_cohort_count": sum(row["primary_cohort"].lower() == "true" for row in analysis_v2),
        "window_completeness": {"first_24_hours": sorted({row["first_window_hour_count"] for row in feature_rows}), "antecedent_days": sorted({row["antecedent_day_count"] for row in feature_rows})},
        "v1_to_v3_precipitation_difference_summary_mm": {column: {"minimum": float(np.min(values)), "maximum": float(np.max(values)), "median": float(np.median(values))} for column, values in differences.items()},
        "outputs": {
            "feature_file": {"file": str(FEATURE_OUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(FEATURE_OUT)},
            "analysis_v2": {"file": str(ANALYSIS_OUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(ANALYSIS_OUT)},
        },
        "interpretation_limit": "Only the three precipitation columns differ between the v1 and v2 analysis tables. The revised values are reanalysis estimates at an anchor tied to satellite detections and do not establish rainfall at an ignition site, wildfire occurrence, physical burned area, or causal mechanisms.",
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"event_count": len(feature_rows), "primary_cohort_count": audit["primary_cohort_count"], "difference_summary": audit["v1_to_v3_precipitation_difference_summary_mm"], "audit": str(AUDIT.relative_to(ROOT))}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
