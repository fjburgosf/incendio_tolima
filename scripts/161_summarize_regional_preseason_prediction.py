"""Summarise the frozen temporal validation without refitting models."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PREDICTIONS_PATH = ROOT / "results" / "primary" / "regional_preseason_prediction_rows_v1.csv"
PANEL_PATH = ROOT / "data" / "processed" / "regional_preseason_prediction_panel_v1.csv"
SUMMARY_CSV = ROOT / "results" / "primary" / "regional_preseason_prediction_summary_v1.csv"
SUMMARY_JSON = ROOT / "results" / "primary" / "regional_preseason_prediction_summary_v1.json"
PRIMARY = "nb_primary_may_july_agricultural"
BASELINE = "baseline_department_historical_mean"


def score(frame: pd.DataFrame) -> dict[str, float]:
    observed = frame["observed_detections"].to_numpy(dtype=float)
    predicted = frame["predicted_detections"].to_numpy(dtype=float)
    errors = observed - predicted
    return {
        "n": int(len(frame)),
        "mae": float(np.mean(np.abs(errors))),
        "rmse": float(np.sqrt(np.mean(errors ** 2))),
        "mean_absolute_log_count_error": float(np.mean(np.abs(np.log((observed + 0.5) / (predicted + 0.5))))),
        "total_observed": float(observed.sum()),
        "total_predicted": float(predicted.sum()),
    }


def empirical_percentile(value: float, reference: np.ndarray) -> float:
    return float(100.0 * np.mean(reference <= value))


def main() -> None:
    if SUMMARY_CSV.exists() or SUMMARY_JSON.exists():
        raise FileExistsError("Summary outputs exist and must not be overwritten")
    predictions = pd.read_csv(PREDICTIONS_PATH)
    panel = pd.read_csv(PANEL_PATH)
    expected = 14 * 8 * 5 + 8 * 5
    if len(predictions) != expected:
        raise RuntimeError(f"Expected {expected} prediction rows, found {len(predictions)}")
    historical = predictions.loc[predictions["scope"] == "historical_leave_one_season_out"].copy()
    final = predictions.loc[predictions["scope"] == "final_2026_holdout"].copy()
    summary_rows: list[dict] = []
    for model, frame in historical.groupby("model", sort=True):
        summary_rows.append({"scope": "historical_pooled_leave_one_season_out", "model": model, **score(frame)})
    for model, frame in final.groupby("model", sort=True):
        summary_rows.append({"scope": "final_2026_all_departments", "model": model, **score(frame)})
    primary_tolima_historical = historical.loc[(historical["model"] == PRIMARY) & (historical["department"] == "Tolima")].copy()
    primary_tolima_final = final.loc[(final["model"] == PRIMARY) & (final["department"] == "Tolima")].copy()
    baseline_tolima_final = final.loc[(final["model"] == BASELINE) & (final["department"] == "Tolima")].copy()
    if len(primary_tolima_final) != 1 or len(baseline_tolima_final) != 1 or len(primary_tolima_historical) != 14:
        raise RuntimeError("Tolima prediction subsets have invalid dimensions")
    for frame in [primary_tolima_historical, primary_tolima_final, baseline_tolima_final]:
        frame["absolute_log_error"] = np.abs(np.log((frame["observed_detections"] + 0.5) / (frame["predicted_detections"] + 0.5)))
    target_error = float(primary_tolima_final["absolute_log_error"].iloc[0])
    historical_errors = primary_tolima_historical["absolute_log_error"].to_numpy(dtype=float)
    tolima_panel = panel.loc[panel["department"] == "Tolima"].copy()
    reference_rain = tolima_panel.loc[tolima_panel["season"] <= 2025, "chirps_may_july_mean_mm"].to_numpy(dtype=float)
    rain_2026 = float(tolima_panel.loc[tolima_panel["season"] == 2026, "chirps_may_july_mean_mm"].iloc[0])
    tolima = {
        "observed_2026_detections": int(primary_tolima_final["observed_detections"].iloc[0]),
        "primary_predicted_2026_detections": float(primary_tolima_final["predicted_detections"].iloc[0]),
        "baseline_predicted_2026_detections": float(baseline_tolima_final["predicted_detections"].iloc[0]),
        "primary_observed_to_predicted_ratio_2026": float(primary_tolima_final["observed_to_predicted_ratio"].iloc[0]),
        "baseline_observed_to_predicted_ratio_2026": float(baseline_tolima_final["observed_to_predicted_ratio"].iloc[0]),
        "primary_absolute_log_error_2026": target_error,
        "historical_tolima_errors_greater_or_equal_2026": int((historical_errors >= target_error).sum()),
        "historical_tolima_folds": int(len(historical_errors)),
        "tolima_may_july_precipitation_2026_mm": rain_2026,
        "tolima_may_july_precipitation_2026_empirical_percentile_vs_2012_2025": empirical_percentile(rain_2026, reference_rain),
    }
    with SUMMARY_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)
    payload = {
        "script": Path(__file__).name,
        "historical_validation": "Each 2012 through 2025 fold holds out all eight departments in one standard processing season. No fold uses 2026.",
        "final_holdout": "All 2026 department rows are predicted from 2012 through 2025 standard processing rows.",
        "pooled_scores": summary_rows,
        "tolima_2026": tolima,
        "limits": [
            "An empirical error comparison across fourteen historical folds is descriptive and not a formal probability of predictability.",
            "The target 2026 detection count is NRT whereas every training outcome is standard processing.",
            "The model contains antecedent precipitation and lagged land cover only. It does not establish causes, ignition mechanisms or operational forecast readiness.",
        ],
    }
    SUMMARY_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"tolima_2026": tolima, "summary_rows": len(summary_rows)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
