"""Fit the frozen primary OLS models with HC3 covariance and BH adjustment."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import numpy as np
import statsmodels.api as sm
from statsmodels.stats.multitest import multipletests


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "analysis_plan_v1.json"
AMENDMENT = ROOT / "configs" / "analysis_plan_amendment_2026-09-06_mapbiomas.json"
INPUT = ROOT / "data" / "processed" / "gwis_event_analysis_v1.csv"
DIAGNOSTICS = ROOT / "results" / "exploratory" / "pre_model_diagnostics_v1.json"
OUTPUT = ROOT / "results" / "primary" / "primary_models_v1.csv"
AUDIT = ROOT / "results" / "primary" / "audit_primary_models_v1.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def main() -> None:
    for target in (OUTPUT, AUDIT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    amendment = json.loads(AMENDMENT.read_text(encoding="utf-8"))
    diagnostics = json.loads(DIAGNOSTICS.read_text(encoding="utf-8"))
    if not diagnostics["joint_vpd_wind_model_allowed"]:
        raise RuntimeError("The frozen model branch requires separate models, not the joint model")
    with INPUT.open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["primary_cohort"].lower() == "true"]
    if len(rows) < plan["model"]["minimum_n_full_model"]:
        raise RuntimeError("Primary cohort is below the frozen full model threshold")
    predictors = ["vpd_mean_first_24h_kpa", "wind_mean_first_24h_ms", "anchor_elevation_m"]
    outcomes = ["log_geometry_area_ha", "log1p_detection_interval_hours"]
    predictor_values = np.asarray([[float(row[name]) for name in predictors] for row in rows], dtype=float)
    if not np.isfinite(predictor_values).all():
        raise RuntimeError("Nonfinite primary predictor")
    median = np.median(predictor_values, axis=0)
    iqr = np.quantile(predictor_values, 0.75, axis=0) - np.quantile(predictor_values, 0.25, axis=0)
    if np.any(iqr <= 0):
        raise RuntimeError("Zero primary predictor IQR")
    design = sm.add_constant((predictor_values - median) / iqr, has_constant="add")
    records = []
    primary_test_positions = []
    model_summaries = {}
    for outcome in outcomes:
        outcome_values = np.asarray([float(row[outcome]) for row in rows], dtype=float)
        if not np.isfinite(outcome_values).all():
            raise RuntimeError(f"Nonfinite outcome {outcome}")
        fit = sm.OLS(outcome_values, design).fit(cov_type="HC3")
        intervals = fit.conf_int(alpha=1 - plan["model"]["confidence_level"])
        model_summaries[outcome] = {
            "n": int(fit.nobs), "r_squared": float(fit.rsquared), "adjusted_r_squared": float(fit.rsquared_adj),
            "residual_degrees_of_freedom": float(fit.df_resid), "covariance": "HC3",
        }
        labels = ["intercept", *predictors]
        for index, label in enumerate(labels):
            record = {
                "outcome": outcome,
                "term": label,
                "coefficient": float(fit.params[index]),
                "robust_standard_error_hc3": float(fit.bse[index]),
                "confidence_interval_95_lower": float(intervals[index, 0]),
                "confidence_interval_95_upper": float(intervals[index, 1]),
                "p_value_two_sided": float(fit.pvalues[index]),
                "bh_q_value_primary_family": "",
                "bh_reject_q_0_05_primary_family": "",
                "n": int(fit.nobs),
                "r_squared": float(fit.rsquared),
                "adjusted_r_squared": float(fit.rsquared_adj),
                "covariance_estimator": "HC3",
                "predictor_scaling": "median and IQR" if label != "intercept" else "not applicable",
                "pre_robustness_interpretation": "requires all mandatory robustness checks",
            }
            if label in predictors[:2]:
                primary_test_positions.append(len(records))
            records.append(record)
    p_values = [records[index]["p_value_two_sided"] for index in primary_test_positions]
    rejected, q_values, _, _ = multipletests(p_values, alpha=plan["multiplicity"]["q_threshold"], method="fdr_bh")
    for index, q_value, reject in zip(primary_test_positions, q_values, rejected, strict=True):
        records[index]["bh_q_value_primary_family"] = float(q_value)
        records[index]["bh_reject_q_0_05_primary_family"] = bool(reject)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    audit = {
        "plan": {"file": str(PLAN.relative_to(ROOT)), "sha256": sha256(PLAN), "version": plan["version"]},
        "amendment": {"file": str(AMENDMENT.relative_to(ROOT)), "sha256": sha256(AMENDMENT), "version": amendment["amendment_version"]},
        "input": {"file": str(INPUT.relative_to(ROOT)), "sha256": sha256(INPUT), "primary_cohort_count": len(rows)},
        "pre_model_diagnostics": {"file": str(DIAGNOSTICS.relative_to(ROOT)), "sha256": sha256(DIAGNOSTICS), "joint_branch_allowed": diagnostics["joint_vpd_wind_model_allowed"]},
        "formula": "transformed GWIS outcome equals intercept plus IQR standardized VPD plus IQR standardized wind plus IQR standardized elevation plus error",
        "outcomes": outcomes,
        "predictors": predictors,
        "model_summaries": model_summaries,
        "multiplicity": {"family_size": len(p_values), "method": "Benjamini Hochberg", "q_threshold": plan["multiplicity"]["q_threshold"], "test_order": [{"outcome": records[index]["outcome"], "term": records[index]["term"]} for index in primary_test_positions]},
        "output": {"file": str(OUTPUT.relative_to(ROOT)), "sha256": sha256(OUTPUT)},
        "interpretation_limit": "These are exploratory associational estimates for operational GWIS outcomes. They do not demonstrate fire occurrence, causation, ignition, physical duration, burned area, or effects before mandatory robustness checks are complete.",
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"n": len(rows), "outcomes": model_summaries, "primary_tests": [{"outcome": records[index]["outcome"], "term": records[index]["term"], "p": records[index]["p_value_two_sided"], "q": records[index]["bh_q_value_primary_family"]} for index in primary_test_positions]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
