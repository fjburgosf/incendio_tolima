"""Fit pre-specified exploratory rainfall associations with GWIS operational outcomes."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import statsmodels.api as sm
from scipy.stats import spearmanr
from statsmodels.stats.multitest import multipletests
from statsmodels.stats.outliers_influence import variance_inflation_factor


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "precipitation_reconstruction_and_exploratory_analysis_plan_v2.json"
DATA = ROOT / "data" / "processed" / "gwis_event_analysis_v2.csv"
MODEL_OUT = ROOT / "results" / "exploratory" / "precipitation_event_models_v1.csv"
CORRELATION_OUT = ROOT / "results" / "exploratory" / "precipitation_event_correlations_v1.csv"
ROBUSTNESS_OUT = ROOT / "results" / "robustness" / "precipitation_event_model_robustness_v1.csv"
LOO_OUT = ROOT / "results" / "robustness" / "precipitation_event_model_leave_one_out_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_precipitation_event_models_v1.json"
EXPOSURES = [
    "precipitation_first_24h_mm",
    "precipitation_previous_7_complete_local_days_mm",
    "maximum_consecutive_dry_days_previous_30_complete_local_days",
]
ADJUSTMENT = ["vpd_mean_first_24h_kpa", "wind_mean_first_24h_ms", "anchor_elevation_m"]
OUTCOMES = ["log_geometry_area_ha", "log1p_detection_interval_hours"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def read_rows() -> list[dict[str, object]]:
    with DATA.open(encoding="utf-8-sig", newline="") as handle:
        source_rows = [row for row in csv.DictReader(handle) if row["primary_cohort"].lower() == "true"]
    if len(source_rows) != 59:
        raise RuntimeError(f"Expected frozen primary cohort of 59 events, found {len(source_rows)}")
    numeric = set(EXPOSURES + ADJUSTMENT + OUTCOMES + ["fraction_geometry_inside_tolima"])
    rows = []
    for row in source_rows:
        item: dict[str, object] = dict(row)
        for column in numeric:
            item[column] = float(item[column])
        item["initial_local_date"] = datetime.fromisoformat(str(row["gwis_initial_datetime_colombia"])).date().isoformat()
        rows.append(item)
    if not np.isfinite(np.asarray([[row[column] for column in numeric] for row in rows], dtype=float)).all():
        raise RuntimeError("Nonfinite input to rainfall-event model")
    return rows


def design(rows: list[dict[str, object]], exposure: str) -> tuple[np.ndarray, dict[str, dict[str, float]], list[str]]:
    labels = [exposure, *ADJUSTMENT]
    values = np.asarray([[float(row[label]) for label in labels] for row in rows], dtype=float)
    medians = np.median(values, axis=0)
    iqr = np.quantile(values, 0.75, axis=0) - np.quantile(values, 0.25, axis=0)
    if np.any(iqr <= 0):
        raise RuntimeError(f"At least one predictor has zero IQR in model for {exposure}")
    scaling = {label: {"median": float(medians[index]), "iqr": float(iqr[index])} for index, label in enumerate(labels)}
    return sm.add_constant((values - medians) / iqr, has_constant="add"), scaling, labels


def fit_hc3(rows: list[dict[str, object]], outcome: str, exposure: str):
    matrix, scaling, labels = design(rows, exposure)
    values = np.asarray([float(row[outcome]) for row in rows], dtype=float)
    return sm.OLS(values, matrix).fit(cov_type="HC3"), scaling, labels, matrix, values


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    for path in (MODEL_OUT, CORRELATION_OUT, ROBUSTNESS_OUT, LOO_OUT, AUDIT):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {path}")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    rows = read_rows()
    correlation_rows: list[dict[str, object]] = []
    for outcome in OUTCOMES:
        for exposure in EXPOSURES:
            correlation, p_value = spearmanr([float(row[exposure]) for row in rows], [float(row[outcome]) for row in rows])
            correlation_rows.append(
                {
                    "outcome": outcome,
                    "exposure": exposure,
                    "spearman_rho": float(correlation),
                    "p_value_two_sided": float(p_value),
                    "n": len(rows),
                    "interpretation": "unadjusted descriptive association only",
                }
            )
    model_rows: list[dict[str, object]] = []
    test_positions: list[int] = []
    model_details: dict[str, object] = {}
    for outcome in OUTCOMES:
        for exposure in EXPOSURES:
            fit, scaling, labels, matrix, _ = fit_hc3(rows, outcome, exposure)
            intervals = fit.conf_int(alpha=0.05)
            vif = {label: float(variance_inflation_factor(matrix[:, 1:], index)) for index, label in enumerate(labels)}
            model_key = f"{outcome}__{exposure}"
            model_details[model_key] = {"n": int(fit.nobs), "r_squared": float(fit.rsquared), "adjusted_r_squared": float(fit.rsquared_adj), "vif": vif}
            for index, label in enumerate(["intercept", *labels]):
                record = {
                    "outcome": outcome,
                    "exposure_model": exposure,
                    "term": label,
                    "coefficient": float(fit.params[index]),
                    "robust_standard_error_hc3": float(fit.bse[index]),
                    "confidence_interval_95_lower": float(intervals[index, 0]),
                    "confidence_interval_95_upper": float(intervals[index, 1]),
                    "p_value_two_sided": float(fit.pvalues[index]),
                    "bh_q_value_rainfall_family": "",
                    "bh_reject_q_0_05_rainfall_family": "",
                    "n": int(fit.nobs),
                    "r_squared": float(fit.rsquared),
                    "adjusted_r_squared": float(fit.rsquared_adj),
                    "predictor_median": scaling.get(label, {}).get("median", ""),
                    "predictor_iqr": scaling.get(label, {}).get("iqr", ""),
                    "vif": vif.get(label, ""),
                    "support_rule_status": "pending robustness",
                }
                if label == exposure:
                    test_positions.append(len(model_rows))
                model_rows.append(record)
    p_values = [float(model_rows[index]["p_value_two_sided"]) for index in test_positions]
    rejected, q_values, _, _ = multipletests(p_values, alpha=0.05, method="fdr_bh")
    for index, q_value, reject in zip(test_positions, q_values, rejected, strict=True):
        model_rows[index]["bh_q_value_rainfall_family"] = float(q_value)
        model_rows[index]["bh_reject_q_0_05_rainfall_family"] = bool(reject)
    robustness_rows: list[dict[str, object]] = []
    loo_rows: list[dict[str, object]] = []
    direction_summary: dict[str, object] = {}
    sensitivity_sets = {
        "exclude_less_or_equal_95_percent_inside_tolima": [row for row in rows if float(row["fraction_geometry_inside_tolima"]) > 0.95],
        "exclude_events_starting_2026_08_31_local": [row for row in rows if str(row["initial_local_date"]) < "2026-08-31"],
    }
    for outcome in OUTCOMES:
        for exposure in EXPOSURES:
            key = f"{outcome}__{exposure}"
            primary_fit, primary_scaling, primary_labels, primary_matrix, primary_values = fit_hc3(rows, outcome, exposure)
            primary_direction = int(np.sign(primary_fit.params[1]))
            checks = {"primary": primary_direction}
            for sensitivity, subset in sensitivity_sets.items():
                fit, _, _, _, _ = fit_hc3(subset, outcome, exposure)
                coefficient = float(fit.params[1])
                checks[sensitivity] = int(np.sign(coefficient))
                robustness_rows.append({"outcome": outcome, "exposure": exposure, "sensitivity": sensitivity, "method": "OLS HC3", "n": int(fit.nobs), "coefficient": coefficient, "direction_matches_primary": int(np.sign(coefficient)) == primary_direction})
            huber = sm.RLM(primary_values, primary_matrix, M=sm.robust.norms.HuberT()).fit(cov="H3")
            huber_coefficient = float(huber.params[1])
            checks["huber_robust_regression"] = int(np.sign(huber_coefficient))
            robustness_rows.append({"outcome": outcome, "exposure": exposure, "sensitivity": "huber_robust_regression", "method": "RLM Huber H3", "n": int(huber.nobs), "coefficient": huber_coefficient, "direction_matches_primary": int(np.sign(huber_coefficient)) == primary_direction})
            loo_directions = []
            for omitted in rows:
                subset = [row for row in rows if row["fire_id"] != omitted["fire_id"]]
                fit, _, _, _, _ = fit_hc3(subset, outcome, exposure)
                coefficient = float(fit.params[1])
                direction = int(np.sign(coefficient))
                loo_directions.append(direction)
                loo_rows.append({"outcome": outcome, "exposure": exposure, "omitted_fire_id": omitted["fire_id"], "coefficient": coefficient, "direction_matches_primary": direction == primary_direction})
            checks["leave_one_event_out"] = sorted(set(loo_directions))
            direction_summary[key] = {"primary_direction": primary_direction, "checks": checks, "leave_one_out_direction_set": sorted(set(loo_directions)), "all_directions_match_primary": all(value == primary_direction for name, value in checks.items() if name != "leave_one_event_out") and set(loo_directions) == {primary_direction}}
    for index in test_positions:
        record = model_rows[index]
        key = f"{record['outcome']}__{record['exposure_model']}"
        ci_excludes_zero = float(record["confidence_interval_95_lower"]) > 0 or float(record["confidence_interval_95_upper"]) < 0
        q_pass = bool(record["bh_reject_q_0_05_rainfall_family"])
        direction_pass = bool(direction_summary[key]["all_directions_match_primary"])
        record["support_rule_status"] = "exploratory_support" if ci_excludes_zero and q_pass and direction_pass else "inconclusive"
    write_csv(CORRELATION_OUT, correlation_rows)
    write_csv(MODEL_OUT, model_rows)
    write_csv(ROBUSTNESS_OUT, robustness_rows)
    write_csv(LOO_OUT, loo_rows)
    audit = {
        "plan": {"file": str(PLAN.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(PLAN)},
        "input": {"file": str(DATA.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(DATA), "primary_cohort_count": len(rows)},
        "correlation": {"method": "Spearman rank", "test_count": len(correlation_rows), "output": {"file": str(CORRELATION_OUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(CORRELATION_OUT)}},
        "models": {"adjustment_set": ADJUSTMENT, "outcomes": OUTCOMES, "rainfall_exposures": EXPOSURES, "family_size": len(test_positions), "covariance": "HC3", "details": model_details, "output": {"file": str(MODEL_OUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(MODEL_OUT)}},
        "robustness": {"checks": list(sensitivity_sets) + ["huber robust regression", "leave one event out"], "direction_summary": direction_summary, "output": {"file": str(ROBUSTNESS_OUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(ROBUSTNESS_OUT)}, "leave_one_out": {"file": str(LOO_OUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(LOO_OUT)}},
        "support_rule_results": [{"outcome": model_rows[index]["outcome"], "exposure": model_rows[index]["exposure_model"], "status": model_rows[index]["support_rule_status"], "q": model_rows[index]["bh_q_value_rainfall_family"]} for index in test_positions],
        "interpretation_limit": "These are exploratory associations of reanalysis-derived rainfall windows with FIRMS-derived GWIS operational attributes. They do not identify causal effects, ignition, wildfire occurrence, physical burned area, or the local accuracy of rainfall estimates.",
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"n": len(rows), "support_rule_results": audit["support_rule_results"], "audit": str(AUDIT.relative_to(ROOT))}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
