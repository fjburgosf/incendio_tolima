"""Fit frozen conditional logistic models for the Tolima case control panel."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from statsmodels.discrete.conditional_models import ConditionalLogit
from statsmodels.tools.sm_exceptions import ConvergenceWarning


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "configs" / "spatial_occurrence_case_control_plan_v1.json"
PANEL_PATH = ROOT / "data" / "processed" / "tolima_spatial_case_control_panel_v2.csv"
OUTPUT_PATH = ROOT / "results" / "primary" / "tolima_spatial_case_control_model_v1.csv"
AUDIT_PATH = ROOT / "results" / "primary" / "audit_tolima_spatial_case_control_model_v1.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def robust_scale(data: pd.DataFrame, column: str) -> tuple[np.ndarray, dict]:
    median = float(data[column].median())
    q1, q3 = data[column].quantile([0.25, 0.75])
    iqr = float(q3 - q1)
    if not math.isfinite(iqr) or iqr <= 0:
        raise RuntimeError(f"Invalid IQR for {column}")
    return ((data[column].to_numpy(dtype=float) - median) / iqr), {"median": median, "iqr": iqr}


def fit(data: pd.DataFrame, model: str, elevation_quartiles: bool) -> tuple[list[dict], dict]:
    slope, slope_scale = robust_scale(data, "slope_degrees")
    frontier, frontier_scale = robust_scale(data, "upra_frontier_fraction")
    columns = []
    names = []
    scales = {"slope_degrees": slope_scale, "upra_frontier_fraction": frontier_scale}
    if elevation_quartiles:
        cutpoints = data["elevation_m"].quantile([0.25, 0.5, 0.75]).to_numpy(dtype=float)
        categories = np.digitize(data["elevation_m"].to_numpy(dtype=float), cutpoints, right=True) + 1
        for category in [2, 3, 4]:
            columns.append((categories == category).astype(float))
            names.append(f"elevation_quartile_{category}_vs_1")
        scales["elevation_quartile_cutpoints_m"] = [float(value) for value in cutpoints]
    else:
        elevation, elevation_scale = robust_scale(data, "elevation_m")
        columns.append(elevation)
        names.append("elevation_m_per_iqr")
        scales["elevation_m"] = elevation_scale
    columns.extend([slope, frontier])
    names.extend(["slope_degrees_per_iqr", "upra_frontier_fraction_per_iqr"])
    exog = np.column_stack(columns)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = ConditionalLogit(data["case"].to_numpy(dtype=int), exog, groups=data["stratum"].to_numpy(dtype=int)).fit(disp=False, maxiter=500)
    failures = [str(item.message) for item in caught if issubclass(item.category, (ConvergenceWarning, RuntimeWarning))]
    if failures:
        raise RuntimeError(f"Conditional logistic convergence failure for {model}: {failures}")
    parameters = np.asarray(result.params, dtype=float)
    intervals = np.asarray(result.conf_int(), dtype=float)
    p_values = np.asarray(result.pvalues, dtype=float)
    rows = []
    for index, name in enumerate(names):
        coefficient = float(parameters[index])
        lower = float(intervals[index, 0])
        upper = float(intervals[index, 1])
        rows.append({
            "model": model,
            "term": name,
            "coefficient_log_odds": coefficient,
            "odds_ratio": float(math.exp(coefficient)),
            "ci95_lower_odds_ratio": float(math.exp(lower)),
            "ci95_upper_odds_ratio": float(math.exp(upper)),
            "p_value_wald": float(p_values[index]),
            "cases": int(data["case"].sum()),
            "controls": int((data["case"] == 0).sum()),
            "strata": int(data["stratum"].nunique()),
        })
    details = {"scales": scales, "log_likelihood": float(result.llf), "converged_without_optimizer_warning": True}
    return rows, details


def main() -> None:
    if OUTPUT_PATH.exists() or AUDIT_PATH.exists():
        raise FileExistsError("Spatial model outputs exist and must not be overwritten")
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    panel = pd.read_csv(PANEL_PATH)
    required = {"case", "stratum", "cell_x", "cell_y", "elevation_m", "slope_degrees", "upra_frontier_fraction"}
    if not required <= set(panel.columns):
        raise RuntimeError("Case control panel has missing required columns")
    if panel.isna().any().any():
        raise RuntimeError("Case control panel contains missing data")
    all_rows, all_details = fit(panel, "primary_linear", elevation_quartiles=False)
    quartile_rows, quartile_details = fit(panel, "sensitivity_elevation_quartiles", elevation_quartiles=True)
    case_rows = panel.loc[panel["case"] == 1].copy()
    case_rows["block_x"] = np.floor_divide(case_rows["cell_x"].to_numpy(dtype=int), 5)
    case_rows["block_y"] = np.floor_divide(case_rows["cell_y"].to_numpy(dtype=int), 5)
    retained_strata = case_rows.sort_values("stratum").drop_duplicates(["block_x", "block_y"], keep="first")["stratum"].tolist()
    cluster_reduced = panel.loc[panel["stratum"].isin(retained_strata)].copy()
    reduced_rows, reduced_details = fit(cluster_reduced, "sensitivity_one_case_per_five_kilometre_block", elevation_quartiles=False)
    rows = all_rows + quartile_rows + reduced_rows
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    audit = {
        "script": Path(__file__).name,
        "plan_sha256": sha256(PLAN_PATH),
        "panel_sha256": sha256(PANEL_PATH),
        "primary": all_details,
        "elevation_quartile_sensitivity": quartile_details,
        "five_kilometre_block_sensitivity": {
            **reduced_details,
            "cases": int(cluster_reduced["case"].sum()),
            "controls": int((cluster_reduced["case"] == 0).sum()),
            "strata": int(cluster_reduced["stratum"].nunique()),
            "case_blocks": int(len(retained_strata)),
        },
        "limits": [
            "Controls are cells without a satellite detection, not confirmed fire free cells.",
            "Conditional odds ratios describe association within matched municipalities, not causal effects or ignition probabilities.",
            "UPRA frontier is a planning boundary, not a direct measure of land use or human activity.",
            "The spatial sensitivity reduces local clustering but does not establish independence of all retained cells.",
        ],
    }
    AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "primary": all_rows,
        "five_kilometre_sensitivity_cases": int(cluster_reduced["case"].sum()),
        "outputs": [str(OUTPUT_PATH.relative_to(ROOT)), str(AUDIT_PATH.relative_to(ROOT))],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
