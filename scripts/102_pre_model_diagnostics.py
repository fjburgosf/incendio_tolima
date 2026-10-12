"""Run outcome independent diagnostics required before primary model fitting."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "analysis_plan_v1.json"
AMENDMENT = ROOT / "configs" / "analysis_plan_amendment_2026-09-06_mapbiomas.json"
INPUT = ROOT / "data" / "processed" / "gwis_event_analysis_v1.csv"
OUTPUT = ROOT / "results" / "exploratory" / "pre_model_diagnostics_v1.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def vif(values: np.ndarray, column: int) -> float:
    target = values[:, column]
    others = np.delete(values, column, axis=1)
    design = np.column_stack((np.ones(len(target)), others))
    fitted = design @ np.linalg.lstsq(design, target, rcond=None)[0]
    residual_sum = float(np.sum((target - fitted) ** 2))
    total_sum = float(np.sum((target - target.mean()) ** 2))
    if total_sum == 0:
        return float("inf")
    r_squared = 1 - residual_sum / total_sum
    return float("inf") if r_squared >= 1 else 1 / (1 - r_squared)


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {OUTPUT}")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    amendment = json.loads(AMENDMENT.read_text(encoding="utf-8"))
    with INPUT.open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["primary_cohort"].lower() == "true"]
    predictors = ["vpd_mean_first_24h_kpa", "wind_mean_first_24h_ms", "anchor_elevation_m"]
    values = np.asarray([[float(row[name]) for name in predictors] for row in rows], dtype=float)
    if len(rows) < plan["model"]["minimum_n_full_model"] or not np.isfinite(values).all():
        raise RuntimeError("Primary pre model gate failed")
    medians = np.median(values, axis=0)
    q25 = np.quantile(values, 0.25, axis=0)
    q75 = np.quantile(values, 0.75, axis=0)
    iqr = q75 - q25
    if np.any(iqr <= 0):
        raise RuntimeError("A primary predictor has zero IQR")
    standardized = (values - medians) / iqr
    if np.linalg.matrix_rank(np.column_stack((np.ones(len(rows)), standardized))) != len(predictors) + 1:
        raise RuntimeError("Primary design matrix is rank deficient")
    vif_values = {name: vif(standardized, index) for index, name in enumerate(predictors)}
    threshold = plan["model"]["vif_joint_model_threshold"]
    joint_vpd_wind_allowed = vif_values[predictors[0]] < threshold and vif_values[predictors[1]] < threshold
    correlation = np.corrcoef(values, rowvar=False)
    result = {
        "plan": {"file": str(PLAN.relative_to(ROOT)), "sha256": sha256(PLAN), "version": plan["version"]},
        "amendment": {"file": str(AMENDMENT.relative_to(ROOT)), "sha256": sha256(AMENDMENT), "version": amendment["amendment_version"]},
        "input": {"file": str(INPUT.relative_to(ROOT)), "sha256": sha256(INPUT)},
        "cohort_n": len(rows),
        "predictors": {
            name: {"median": float(medians[index]), "q25": float(q25[index]), "q75": float(q75[index]), "iqr": float(iqr[index]), "vif": vif_values[name]}
            for index, name in enumerate(predictors)
        },
        "pearson_correlation_matrix": {name: {other: float(correlation[row_index, col_index]) for col_index, other in enumerate(predictors)} for row_index, name in enumerate(predictors)},
        "vif_threshold": threshold,
        "joint_vpd_wind_model_allowed": joint_vpd_wind_allowed,
        "model_branch_required_by_plan": "joint VPD plus wind plus elevation" if joint_vpd_wind_allowed else "separate VPD and wind models plus elevation",
        "interpretation_limit": "These diagnostics use only planned predictors to select the predeclared model branch. They do not inspect associations with either GWIS outcome and do not establish any effect.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cohort_n": len(rows), "vif": vif_values, "joint_vpd_wind_model_allowed": joint_vpd_wind_allowed, "model_branch": result["model_branch_required_by_plan"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
