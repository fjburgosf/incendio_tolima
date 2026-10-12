"""Execute the mandatory robustness and falsification checks for D1."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import statsmodels.api as sm
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "analysis_plan_v1.json"
AMENDMENT = ROOT / "configs" / "analysis_plan_amendment_2026-09-06_mapbiomas.json"
DATA = ROOT / "data" / "processed" / "gwis_event_analysis_v1.csv"
COHORT = ROOT / "data" / "interim" / "gwis_nrt_burned_area_august_cohort_refreshed_2026-09-06.geojson"
PRIMARY = ROOT / "results" / "primary" / "primary_models_v1.csv"
OUTPUT = ROOT / "results" / "robustness" / "primary_robustness_v1.csv"
LOO_OUTPUT = ROOT / "results" / "robustness" / "primary_leave_one_out_v1.csv"
OVERLAP_OUTPUT = ROOT / "results" / "robustness" / "primary_overlap_component_selection_v1.csv"
AUDIT = ROOT / "results" / "robustness" / "audit_primary_robustness_v1.json"
SEED = 20260906


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def to_float_rows(rows: list[dict]) -> list[dict]:
    numeric_columns = [
        "gwis_geometry_area_ha", "gwis_reported_area_ha", "gwis_tolima_intersection_area_ha",
        "fraction_geometry_inside_tolima", "log_geometry_area_ha", "log1p_detection_interval_hours",
        "vpd_mean_first_24h_kpa", "vpd_p95_first_24h_kpa", "wind_mean_first_24h_ms", "wind_p95_first_24h_ms",
        "temperature_mean_first_24h_c", "anchor_elevation_m", "anchor_longitude", "anchor_latitude",
    ]
    converted = []
    for row in rows:
        item = dict(row)
        for column in numeric_columns:
            item[column] = float(item[column])
        item["primary_cohort"] = item["primary_cohort"].lower() == "true"
        item["pre_august_start_flag"] = item["pre_august_start_flag"].lower() == "true"
        item["initial_local_date"] = item["gwis_initial_datetime_colombia"][:10]
        converted.append(item)
    return converted


def standardised_design(rows: list[dict], exposure_columns: list[str]) -> tuple[np.ndarray, dict[str, dict[str, float]]]:
    columns = [*exposure_columns, "anchor_elevation_m"]
    values = np.asarray([[row[column] for column in columns] for row in rows], dtype=float)
    medians = np.median(values, axis=0)
    iqr = np.quantile(values, 0.75, axis=0) - np.quantile(values, 0.25, axis=0)
    if np.any(iqr <= 0) or not np.isfinite(values).all():
        raise RuntimeError("Invalid predictor distribution in robustness model")
    scaling = {column: {"median": float(medians[index]), "iqr": float(iqr[index])} for index, column in enumerate(columns)}
    return sm.add_constant((values - medians) / iqr, has_constant="add"), scaling


def fit_ols(rows: list[dict], outcome: str, exposure_columns: list[str], covariance: str = "HC3", groups=None) -> tuple[object, dict[str, dict[str, float]]]:
    design, scaling = standardised_design(rows, exposure_columns)
    values = np.asarray([row[outcome] for row in rows], dtype=float)
    if not np.isfinite(values).all():
        raise RuntimeError(f"Invalid outcome for {outcome}")
    if covariance == "cluster":
        fit = sm.OLS(values, design).fit(cov_type="cluster", cov_kwds={"groups": groups, "use_correction": True, "df_correction": True}, use_t=True)
    else:
        fit = sm.OLS(values, design).fit(cov_type="HC3")
    return fit, scaling


def extract_records(label: str, rows: list[dict], outcome: str, exposure_columns: list[str], fit, scaling: dict, method: str) -> list[dict]:
    labels = ["intercept", *exposure_columns, "anchor_elevation_m"]
    intervals = fit.conf_int(alpha=0.05) if hasattr(fit, "conf_int") else None
    result = []
    for index, term in enumerate(labels):
        result.append({
            "sensitivity": label,
            "method": method,
            "outcome": outcome,
            "term": term,
            "n": int(fit.nobs),
            "coefficient": float(fit.params[index]),
            "standard_error": float(fit.bse[index]) if hasattr(fit, "bse") else "",
            "confidence_interval_95_lower": float(intervals[index, 0]) if intervals is not None else "",
            "confidence_interval_95_upper": float(intervals[index, 1]) if intervals is not None else "",
            "p_value_two_sided": float(fit.pvalues[index]) if hasattr(fit, "pvalues") else "",
            "r_squared": float(fit.rsquared) if hasattr(fit, "rsquared") else "",
            "predictor_median": scaling.get(term, {}).get("median", ""),
            "predictor_iqr": scaling.get(term, {}).get("iqr", ""),
        })
    return result


def overlap_components(rows: list[dict]) -> tuple[dict[str, int], list[dict]]:
    by_id = {row["fire_id"]: row for row in rows}
    cohort = json.loads(COHORT.read_text(encoding="utf-8"))
    project = Transformer.from_crs(4326, 32618, always_xy=True).transform
    geometries = {str(feature["properties"]["fire_id"]): transform(project, shape(feature["geometry"])) for feature in cohort["features"] if str(feature["properties"]["fire_id"]) in by_id}
    if set(geometries) != set(by_id):
        raise RuntimeError("Primary cohort geometries do not match analysis table")
    adjacency = {fire_id: set() for fire_id in by_id}
    identifiers = sorted(geometries)
    for index, first in enumerate(identifiers):
        for second in identifiers[index + 1:]:
            if geometries[first].intersection(geometries[second]).area > 100.0:
                adjacency[first].add(second)
                adjacency[second].add(first)
    components, component_id = [], {}
    for fire_id in identifiers:
        if fire_id in component_id:
            continue
        pending, members = [fire_id], []
        next_id = len(components) + 1
        while pending:
            current = pending.pop()
            if current in component_id:
                continue
            component_id[current] = next_id
            members.append(current)
            pending.extend(sorted(adjacency[current] - set(component_id)))
        components.append(sorted(members))
    bridge = []
    for members in components:
        ordered = sorted(members, key=lambda fire_id: (by_id[fire_id]["gwis_initial_datetime_utc"], fire_id))
        for rank, fire_id in enumerate(ordered, start=1):
            bridge.append({"fire_id": fire_id, "overlap_component_id": component_id[fire_id], "component_size": len(members), "temporal_rank_within_component": rank, "retained_for_overlap_sensitivity": rank == 1})
    return component_id, bridge


def haversine_matrix(rows: list[dict]) -> np.ndarray:
    longitude = np.radians(np.asarray([row["anchor_longitude"] for row in rows], dtype=float))
    latitude = np.radians(np.asarray([row["anchor_latitude"] for row in rows], dtype=float))
    delta_lon = longitude[:, None] - longitude[None, :]
    delta_lat = latitude[:, None] - latitude[None, :]
    value = np.sin(delta_lat / 2) ** 2 + np.cos(latitude[:, None]) * np.cos(latitude[None, :]) * np.sin(delta_lon / 2) ** 2
    return 6_371_000.0 * 2 * np.arcsin(np.sqrt(np.clip(value, 0, 1)))


def moran_i(residual: np.ndarray, weights: np.ndarray) -> float:
    centred = residual - residual.mean()
    return float((len(residual) / weights.sum()) * ((weights * np.outer(centred, centred)).sum() / (centred ** 2).sum()))


def moran_permutation(rows: list[dict], residual: np.ndarray) -> dict:
    distances = haversine_matrix(rows)
    n = len(rows)
    weights = np.zeros((n, n), dtype=float)
    for index in range(n):
        nearest = np.argsort(distances[index])[1:5]
        weights[index, nearest] = 1.0
    weights = np.maximum(weights, weights.T)
    weights = weights / weights.sum(axis=1, keepdims=True)
    observed = moran_i(residual, weights)
    generator = np.random.default_rng(SEED)
    permutations = np.asarray([moran_i(generator.permutation(residual), weights) for _ in range(9999)], dtype=float)
    p_two_sided = (1 + int(np.count_nonzero(np.abs(permutations) >= abs(observed)))) / (len(permutations) + 1)
    return {"n": n, "k": 4, "symmetric_weights": True, "row_standardized": True, "permutations": 9999, "seed": SEED, "observed_moran_i": observed, "permutation_two_sided_p_value": p_two_sided, "permutation_minimum": float(permutations.min()), "permutation_maximum": float(permutations.max())}


def main() -> None:
    for target in (OUTPUT, LOO_OUTPUT, OVERLAP_OUTPUT, AUDIT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    amendment = json.loads(AMENDMENT.read_text(encoding="utf-8"))
    all_rows = to_float_rows(read_rows(DATA))
    primary_rows = [row for row in all_rows if row["primary_cohort"]]
    if len(primary_rows) != 59:
        raise RuntimeError(f"Expected 59 primary events, found {len(primary_rows)}")
    records = []
    primary_exposures = ["vpd_mean_first_24h_kpa", "wind_mean_first_24h_ms"]
    outcomes = ["log_geometry_area_ha", "log1p_detection_interval_hours"]
    for outcome in outcomes:
        fit, scaling = fit_ols(primary_rows, outcome, primary_exposures)
        records.extend(extract_records("primary_reproduction", primary_rows, outcome, primary_exposures, fit, scaling, "OLS HC3"))

    alternate_area = {
        "log_tolima_intersection_area_ha": lambda row: math.log(row["gwis_tolima_intersection_area_ha"]),
        "log_reported_area_ha": lambda row: math.log(row["gwis_reported_area_ha"]),
    }
    for outcome, construction in alternate_area.items():
        sensitivity_rows = [dict(row, **{outcome: construction(row)}) for row in primary_rows]
        fit, scaling = fit_ols(sensitivity_rows, outcome, primary_exposures)
        records.extend(extract_records(f"alternative_outcome_{outcome}", sensitivity_rows, outcome, primary_exposures, fit, scaling, "OLS HC3"))

    inside_95 = [row for row in primary_rows if row["fraction_geometry_inside_tolima"] > 0.95]
    for outcome in outcomes:
        fit, scaling = fit_ols(inside_95, outcome, primary_exposures)
        records.extend(extract_records("restrict_more_than_95_percent_inside_tolima", inside_95, outcome, primary_exposures, fit, scaling, "OLS HC3"))

    include_pre = [row for row in all_rows if row["fire_id"] != "15556230"]
    for outcome in outcomes:
        fit, scaling = fit_ols(include_pre, outcome, primary_exposures)
        records.extend(extract_records("include_pre_august_active_events", include_pre, outcome, primary_exposures, fit, scaling, "OLS HC3"))

    exclude_aug31 = [row for row in primary_rows if row["initial_local_date"] < "2026-08-31"]
    for outcome in outcomes:
        fit, scaling = fit_ols(exclude_aug31, outcome, primary_exposures)
        records.extend(extract_records("exclude_events_starting_2026_08_31_local", exclude_aug31, outcome, primary_exposures, fit, scaling, "OLS HC3"))

    for outcome in outcomes:
        fit, scaling = fit_ols(primary_rows, outcome, ["temperature_mean_first_24h_c", "wind_mean_first_24h_ms"])
        records.extend(extract_records("replace_vpd_with_mean_temperature", primary_rows, outcome, ["temperature_mean_first_24h_c", "wind_mean_first_24h_ms"], fit, scaling, "OLS HC3"))
        fit, scaling = fit_ols(primary_rows, outcome, ["vpd_p95_first_24h_kpa", "wind_p95_first_24h_ms"])
        records.extend(extract_records("replace_means_with_p95", primary_rows, outcome, ["vpd_p95_first_24h_kpa", "wind_p95_first_24h_ms"], fit, scaling, "OLS HC3"))

    for outcome in outcomes:
        design, scaling = standardised_design(primary_rows, primary_exposures)
        values = np.asarray([row[outcome] for row in primary_rows])
        fit = sm.RLM(values, design, M=sm.robust.norms.HuberT()).fit(cov="H3")
        records.extend(extract_records("huber_robust_regression", primary_rows, outcome, primary_exposures, fit, scaling, "RLM Huber H3"))

    loo_rows = []
    for outcome in outcomes:
        for omitted in primary_rows:
            subset = [row for row in primary_rows if row["fire_id"] != omitted["fire_id"]]
            fit, _ = fit_ols(subset, outcome, primary_exposures)
            loo_rows.append({"outcome": outcome, "omitted_fire_id": omitted["fire_id"], "vpd_coefficient": float(fit.params[1]), "wind_coefficient": float(fit.params[2]), "elevation_coefficient": float(fit.params[3])})

    _, bridge = overlap_components(primary_rows)
    retained_ids = {row["fire_id"] for row in bridge if row["retained_for_overlap_sensitivity"]}
    overlap_rows = [row for row in primary_rows if row["fire_id"] in retained_ids]
    for outcome in outcomes:
        fit, scaling = fit_ols(overlap_rows, outcome, primary_exposures)
        records.extend(extract_records("one_event_per_spatial_overlap_component", overlap_rows, outcome, primary_exposures, fit, scaling, "OLS HC3"))

    cluster_group_counts = defaultdict(int)
    for row in primary_rows:
        cluster_group_counts[row["initial_local_date"]] += 1
    groups = np.asarray([row["initial_local_date"] for row in primary_rows])
    for outcome in outcomes:
        fit, scaling = fit_ols(primary_rows, outcome, primary_exposures, covariance="cluster", groups=groups)
        records.extend(extract_records("cluster_by_local_start_date_small_sample_correction", primary_rows, outcome, primary_exposures, fit, scaling, "OLS cluster by local start date"))

    moran = {}
    for outcome in outcomes:
        fit, _ = fit_ols(primary_rows, outcome, primary_exposures)
        moran[outcome] = moran_permutation(primary_rows, np.asarray(fit.resid, dtype=float))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    with LOO_OUTPUT.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(loo_rows[0]))
        writer.writeheader()
        writer.writerows(loo_rows)
    with OVERLAP_OUTPUT.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(bridge[0]))
        writer.writeheader()
        writer.writerows(sorted(bridge, key=lambda row: (row["overlap_component_id"], row["temporal_rank_within_component"])))
    loo_summary = {}
    for outcome in outcomes:
        subset = [row for row in loo_rows if row["outcome"] == outcome]
        loo_summary[outcome] = {
            "vpd_minimum": min(row["vpd_coefficient"] for row in subset), "vpd_maximum": max(row["vpd_coefficient"] for row in subset),
            "wind_minimum": min(row["wind_coefficient"] for row in subset), "wind_maximum": max(row["wind_coefficient"] for row in subset),
            "vpd_signs": sorted({int(np.sign(row["vpd_coefficient"])) for row in subset}), "wind_signs": sorted({int(np.sign(row["wind_coefficient"])) for row in subset}),
        }
    audit = {
        "plan": {"file": str(PLAN.relative_to(ROOT)), "sha256": sha256(PLAN), "version": plan["version"]},
        "amendment": {"file": str(AMENDMENT.relative_to(ROOT)), "sha256": sha256(AMENDMENT), "version": amendment["amendment_version"]},
        "data": {"file": str(DATA.relative_to(ROOT)), "sha256": sha256(DATA), "primary_n": len(primary_rows)},
        "primary_output_reproduced": {"file": str(PRIMARY.relative_to(ROOT)), "sha256": sha256(PRIMARY)},
        "mandatory_checks_executed": [
            "alternative area outcomes", "greater than 95 percent inside Tolima", "include pre August active events", "exclude August 31 starts", "temperature replacing VPD", "P95 VPD and wind", "Huber robust regression", "leave one out", "one event per spatial overlap component", "cluster covariance by local start date", "Moran residual permutation test"
        ],
        "sample_sizes": {"primary": len(primary_rows), "inside_95": len(inside_95), "include_pre_august": len(include_pre), "exclude_august_31": len(exclude_aug31), "overlap_component_retained": len(overlap_rows)},
        "overlap_components": {"component_count": len({row["overlap_component_id"] for row in bridge}), "records": len(bridge), "removed": len(primary_rows) - len(overlap_rows), "selection_file": str(OVERLAP_OUTPUT.relative_to(ROOT)), "sha256": sha256(OVERLAP_OUTPUT)},
        "local_start_date_clusters": {"cluster_count": len(cluster_group_counts), "size_by_date": dict(sorted(cluster_group_counts.items()))},
        "leave_one_out": {"file": str(LOO_OUTPUT.relative_to(ROOT)), "sha256": sha256(LOO_OUTPUT), "summary": loo_summary},
        "moran_residual": moran,
        "output": {"file": str(OUTPUT.relative_to(ROOT)), "sha256": sha256(OUTPUT)},
        "interpretation_limit": "Sensitivity agreement only tests stability under the listed operational choices. It cannot remove dependence between FIRMS derived GWIS outcomes, establish a counterfactual, prove physical burned area or duration, or support causal claims.",
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"sample_sizes": audit["sample_sizes"], "overlap_removed": audit["overlap_components"]["removed"], "leave_one_out": loo_summary, "moran_residual": moran}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
