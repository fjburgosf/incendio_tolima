"""Evaluate frozen preseason prediction models without temporal leakage.

The unit is department by August. Historical leave one season out folds only use
standard processing seasons from 2012 through 2025. August 2026 is held out
once, after fitting on all standard processing seasons. This script predicts
satellite detections, not fires, ignitions, burned area or causal effects.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf
from statsmodels.tools.sm_exceptions import ConvergenceWarning


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "configs" / "regional_preseason_prediction_plan_v1.json"
PANEL_PATH = ROOT / "data" / "processed" / "regional_preseason_prediction_panel_v1.csv"
PREDICTIONS_PATH = ROOT / "results" / "primary" / "regional_preseason_prediction_rows_v1.csv"
METRICS_PATH = ROOT / "results" / "primary" / "regional_preseason_prediction_metrics_v1.csv"
AUDIT_PATH = ROOT / "results" / "primary" / "audit_regional_preseason_prediction_v1.json"

HISTORICAL_SEASONS = list(range(2012, 2026))
TARGET_SEASON = 2026
MODEL_SPECS = {
    "nb_primary_may_july_agricultural": {
        "family": "negative_binomial",
        "rain": "chirps_may_july_mean_mm",
        "cover": "mapbiomas_agricultural_fraction_lagged",
        "role": "primary",
    },
    "poisson_primary_may_july_agricultural": {
        "family": "poisson",
        "rain": "chirps_may_july_mean_mm",
        "cover": "mapbiomas_agricultural_fraction_lagged",
        "role": "distributional_sensitivity",
    },
    "nb_sensitivity_july_agricultural": {
        "family": "negative_binomial",
        "rain": "chirps_july_mean_mm",
        "cover": "mapbiomas_agricultural_fraction_lagged",
        "role": "physical_sensitivity",
    },
    "nb_sensitivity_may_july_natural_nonforest": {
        "family": "negative_binomial",
        "rain": "chirps_may_july_mean_mm",
        "cover": "mapbiomas_natural_nonforest_fraction_lagged",
        "role": "coverage_sensitivity",
    },
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def robust_scale(train: pd.DataFrame, test: pd.DataFrame, columns: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    train_out = train.copy()
    test_out = test.copy()
    constants = {}
    for column in columns:
        median = float(train_out[column].median())
        q1, q3 = train_out[column].quantile([0.25, 0.75])
        iqr = float(q3 - q1)
        if not math.isfinite(iqr) or iqr <= 0:
            raise RuntimeError(f"Non positive training IQR for {column}")
        scaled = f"scaled_{column}"
        train_out[scaled] = (train_out[column] - median) / iqr
        test_out[scaled] = (test_out[column] - median) / iqr
        constants[column] = {"median": median, "iqr": iqr}
    return train_out, test_out, constants


def fit_predict(spec: dict, train: pd.DataFrame, test: pd.DataFrame) -> tuple[np.ndarray, dict]:
    rain = spec["rain"]
    cover = spec["cover"]
    train_scaled, test_scaled, constants = robust_scale(train, test, [rain, cover])
    formula = f"detections ~ scaled_{rain} + scaled_{cover} + C(department)"
    warnings_seen: list[str] = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        if spec["family"] == "negative_binomial":
            result = smf.negativebinomial(formula, data=train_scaled).fit(disp=False, maxiter=500)
            converged = bool(result.mle_retvals.get("converged", False))
        elif spec["family"] == "poisson":
            result = smf.glm(formula, data=train_scaled, family=sm.families.Poisson()).fit()
            converged = bool(result.converged)
        else:
            raise RuntimeError(f"Unknown family {spec['family']}")
        warnings_seen = [str(item.message) for item in caught]
    if not converged:
        raise RuntimeError(f"Model did not converge for {spec['family']}")
    if any(issubclass(item.category, ConvergenceWarning) for item in caught):
        raise RuntimeError(f"Convergence warning for {spec['family']}")
    prediction = np.asarray(result.predict(test_scaled), dtype=float)
    if not np.isfinite(prediction).all() or (prediction < 0).any():
        raise RuntimeError(f"Invalid predictions for {spec['family']}")
    details = {
        "formula": formula,
        "converged": converged,
        "warnings": warnings_seen,
        "scaling": constants,
        "alpha": float(result.params["alpha"]) if spec["family"] == "negative_binomial" else None,
    }
    return prediction, details


def metrics(observed: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    errors = observed - predicted
    return {
        "n": int(len(observed)),
        "mae": float(np.mean(np.abs(errors))),
        "rmse": float(np.sqrt(np.mean(errors ** 2))),
        "mean_absolute_log_count_error": float(np.mean(np.abs(np.log((observed + 0.5) / (predicted + 0.5))))),
        "mean_observed_to_predicted_ratio": float(np.mean((observed + 0.5) / (predicted + 0.5))),
        "total_observed": float(np.sum(observed)),
        "total_predicted": float(np.sum(predicted)),
    }


def baseline_predict(train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    means = train.groupby("department", observed=True)["detections"].mean()
    if not set(test["department"]) <= set(means.index):
        raise RuntimeError("A held out department is absent from training")
    return test["department"].map(means).to_numpy(dtype=float)


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise RuntimeError(f"No rows to write to {path}")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    if PREDICTIONS_PATH.exists() or METRICS_PATH.exists() or AUDIT_PATH.exists():
        raise FileExistsError("Prediction outputs exist and must not be overwritten")
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    panel = pd.read_csv(PANEL_PATH)
    expected_rows = len(plan["geography"]["departments"]) * len(plan["temporal_design"]["seasons"])
    if len(panel) != expected_rows:
        raise RuntimeError("Panel row count differs from the frozen design")
    if panel.isna().any().any():
        allowed = {"gini_of_cell_counts"}
        unexpected = [name for name in panel.columns if panel[name].isna().any() and name not in allowed]
        if unexpected:
            raise RuntimeError(f"Unexpected missing fields: {unexpected}")
    if set(panel.loc[panel["season"] <= 2025, "processing_level"]) != {"SP"}:
        raise RuntimeError("Historical panel contains non standard processing rows")
    if set(panel.loc[panel["season"] == TARGET_SEASON, "processing_level"]) != {"NRT"}:
        raise RuntimeError("Target season is not labelled NRT")
    historical = panel.loc[panel["season"].isin(HISTORICAL_SEASONS)].copy()
    target = panel.loc[panel["season"] == TARGET_SEASON].copy()
    if len(historical) != 112 or len(target) != 8:
        raise RuntimeError("Historical or target panel dimensions are invalid")

    poisson_department = smf.glm("detections ~ C(department)", data=historical, family=sm.families.Poisson()).fit()
    poisson_dispersion = float(poisson_department.pearson_chi2 / poisson_department.df_resid)
    predictor_columns = [
        "chirps_july_mean_mm", "chirps_may_july_mean_mm",
        "mapbiomas_forest_fraction_lagged", "mapbiomas_natural_nonforest_fraction_lagged",
        "mapbiomas_agricultural_fraction_lagged",
    ]
    correlations = historical[predictor_columns].corr().round(6).to_dict()

    prediction_rows: list[dict] = []
    metric_rows: list[dict] = []
    fit_details: dict[str, dict] = {}
    evaluations: list[tuple[str, int, pd.DataFrame, pd.DataFrame]] = []
    for season in HISTORICAL_SEASONS:
        train = historical.loc[historical["season"] != season].copy()
        test = historical.loc[historical["season"] == season].copy()
        evaluations.append(("historical_leave_one_season_out", season, train, test))
    evaluations.append(("final_2026_holdout", TARGET_SEASON, historical.copy(), target.copy()))

    for scope, held_out_season, train, test in evaluations:
        observed = test["detections"].to_numpy(dtype=float)
        baseline = baseline_predict(train, test)
        models = {"baseline_department_historical_mean": (baseline, {"family": "baseline"})}
        for model_name, spec in MODEL_SPECS.items():
            predicted, details = fit_predict(spec, train, test)
            models[model_name] = (predicted, details)
            fit_details[f"{scope}_{held_out_season}_{model_name}"] = details
        for model_name, (predicted, details) in models.items():
            for row, value in zip(test.to_dict("records"), predicted, strict=True):
                prediction_rows.append(
                    {
                        "scope": scope,
                        "held_out_season": held_out_season,
                        "model": model_name,
                        "department": row["department"],
                        "observed_detections": int(row["detections"]),
                        "predicted_detections": round(float(value), 8),
                        "observed_to_predicted_ratio": round((float(row["detections"]) + 0.5) / (float(value) + 0.5), 8),
                        "processing_level": row["processing_level"],
                    }
                )
            summary = metrics(observed, predicted)
            metric_rows.append(
                {
                    "scope": scope,
                    "held_out_season": held_out_season,
                    "model": model_name,
                    **{key: round(value, 8) if isinstance(value, float) else value for key, value in summary.items()},
                }
            )

    PREDICTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_csv(PREDICTIONS_PATH, prediction_rows)
    write_csv(METRICS_PATH, metric_rows)
    historical_summary = []
    for model_name in ["baseline_department_historical_mean", *MODEL_SPECS]:
        subset = [row for row in metric_rows if row["scope"] == "historical_leave_one_season_out" and row["model"] == model_name]
        historical_summary.append(
            {
                "model": model_name,
                **metrics(
                    np.array([row["total_observed"] for row in subset]),
                    np.array([row["total_predicted"] for row in subset]),
                ),
                "interpretation": "season total comparison across leave one season out folds, not an inference test",
            }
        )
    audit = {
        "script": Path(__file__).name,
        "plan_sha256": sha256(PLAN_PATH),
        "panel_sha256": sha256(PANEL_PATH),
        "historical_seasons": HISTORICAL_SEASONS,
        "target_season": TARGET_SEASON,
        "historical_rows": int(len(historical)),
        "target_rows": int(len(target)),
        "zero_detections_historical": int((historical["detections"] == 0).sum()),
        "zero_detection_fraction_historical": float((historical["detections"] == 0).mean()),
        "poisson_department_pearson_dispersion": poisson_dispersion,
        "predictor_correlations_historical": correlations,
        "models": MODEL_SPECS,
        "historical_season_total_summary": historical_summary,
        "fit_details": fit_details,
        "outputs": {
            "prediction_rows": {"path": str(PREDICTIONS_PATH.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(PREDICTIONS_PATH)},
            "metrics": {"path": str(METRICS_PATH.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(METRICS_PATH)},
        },
        "limits": [
            "Prediction targets VIIRS Suomi NPP satellite detections, not physical fire occurrence or cause.",
            "2026 uses NRT while 2012 through 2025 use standard processing. Direct calibration remains unavailable.",
            "CHIRPS precipitation is not soil moisture and May through July is a 92 day calendar total.",
            "MapBiomas 2025 and 2026 features carry 2024 cover and remain flagged in the panel.",
            "Model comparisons are predefined descriptive predictive checks, not causal estimates or significance tests.",
        ],
    }
    AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    final_primary = [
        row for row in metric_rows
        if row["scope"] == "final_2026_holdout" and row["model"] == "nb_primary_may_july_agricultural"
    ][0]
    print(json.dumps({
        "historical_rows": len(historical),
        "target_rows": len(target),
        "poisson_department_pearson_dispersion": round(poisson_dispersion, 4),
        "final_primary_2026": final_primary,
        "outputs": [str(PREDICTIONS_PATH.relative_to(ROOT)), str(METRICS_PATH.relative_to(ROOT)), str(AUDIT_PATH.relative_to(ROOT))],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
