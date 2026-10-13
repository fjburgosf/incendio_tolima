"""Evalúa la enmienda ENSO del modelo regional de anticipación.

Sigue `configs/regional_preseason_prediction_enso_amendment_v1.json`, congelado
antes de ajustar cualquier modelo con índices ENSO. Usa la misma familia,
validación, escalado y controles de convergencia que el script 160. Antes de
evaluar la enmienda reproduce las predicciones del modelo primario original.
Todo el resultado es exploratorio y posterior.
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
import statsmodels.formula.api as smf
from scipy import stats
from statsmodels.tools.sm_exceptions import ConvergenceWarning


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "regional_preseason_prediction_enso_amendment_v1.json"
PANEL = ROOT / "data" / "processed" / "regional_preseason_prediction_panel_v1.csv"
ENSO = ROOT / "data" / "interim" / "enso_indices_2012_2026_v1.csv"
ORIGINAL_ROWS = ROOT / "results" / "primary" / "regional_preseason_prediction_rows_v1.csv"
ROWS_OUT = ROOT / "results" / "exploratory" / "regional_enso_prediction_rows_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_regional_enso_prediction_v1.json"
HISTORICAL = list(range(2012, 2026))
TARGET = 2026
RAIN = "chirps_may_july_mean_mm"
COVER = "mapbiomas_agricultural_fraction_lagged"
INDICES = {"oni_mjj": "primary", "roni_mjj": "sensitivity", "oni_amj": "sensitivity"}
SEED = 20261012
PERMUTATIONS = 100000


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def scale(train: pd.DataFrame, test: pd.DataFrame, columns: list[str]):
    train, test = train.copy(), test.copy()
    for column in columns:
        median = float(train[column].median())
        q1, q3 = train[column].quantile([0.25, 0.75])
        iqr = float(q3 - q1)
        if not math.isfinite(iqr) or iqr <= 0:
            raise RuntimeError(f"Rango intercuartílico no positivo para {column}")
        train[f"scaled_{column}"] = (train[column] - median) / iqr
        test[f"scaled_{column}"] = (test[column] - median) / iqr
    return train, test


def fit_predict(predictors: list[str], train: pd.DataFrame, test: pd.DataFrame) -> tuple[np.ndarray, dict]:
    train_s, test_s = scale(train, test, predictors)
    formula = "detections ~ " + " + ".join([f"scaled_{p}" for p in predictors] + ["C(department)"])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = smf.negativebinomial(formula, data=train_s).fit(disp=False, maxiter=500)
    if not bool(result.mle_retvals.get("converged", False)):
        raise RuntimeError(f"Sin convergencia, {formula}")
    if any(issubclass(item.category, ConvergenceWarning) for item in caught):
        raise RuntimeError(f"Advertencia de convergencia, {formula}")
    prediction = np.asarray(result.predict(test_s), dtype=float)
    if not np.isfinite(prediction).all() or (prediction < 0).any():
        raise RuntimeError(f"Predicción inválida, {formula}")
    coefficients = {p: float(result.params[f"scaled_{p}"]) for p in predictors}
    return prediction, {"formula": formula, "alpha": float(result.params["alpha"]), "scaled_coefficients": coefficients}


def log_error(observed, predicted):
    return np.log((np.asarray(observed, float) + 0.5) / (np.asarray(predicted, float) + 0.5))


def summary(observed: np.ndarray, predicted: np.ndarray) -> dict:
    errors = observed - predicted
    return {
        "n": int(observed.size),
        "mean_absolute_log_count_error": float(np.mean(np.abs(log_error(observed, predicted)))),
        "mae": float(np.mean(np.abs(errors))),
        "rmse": float(np.sqrt(np.mean(errors ** 2))),
    }


def benjamini_hochberg(p_values: list[float]) -> list[float]:
    p = np.asarray(p_values, float)
    order = np.argsort(p)
    ranked = p[order] * p.size / (np.arange(p.size) + 1)
    adjusted = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty_like(p)
    out[order] = np.minimum(adjusted, 1.0)
    return out.tolist()


def main() -> None:
    if AUDIT.exists() or ROWS_OUT.exists():
        raise FileExistsError("Las salidas ya existen y no se sobrescriben")
    panel = pd.read_csv(PANEL)
    enso = pd.read_csv(ENSO)
    panel = panel.merge(enso, on="season", how="left", validate="many_to_one")
    if panel[list(INDICES)].isna().any().any():
        raise RuntimeError("Falta un índice ENSO en el panel")
    historical = panel[panel["season"].isin(HISTORICAL)].copy()
    target = panel[panel["season"] == TARGET].copy()
    if len(historical) != 112 or len(target) != 8:
        raise RuntimeError("Dimensiones del panel distintas del diseño")

    models = {"nb_primary_may_july_agricultural": [RAIN, COVER]}
    for index in INDICES:
        models[f"nb_enso_only_{index}"] = [index]
        models[f"nb_enso_plus_primary_{index}"] = [index, RAIN, COVER]

    folds = [("historical_leave_one_season_out", s, historical[historical["season"] != s], historical[historical["season"] == s]) for s in HISTORICAL]
    folds.append(("final_2026_holdout", TARGET, historical, target))

    rows, details = [], {}
    for scope, season, train, test in folds:
        means = train.groupby("department")["detections"].mean()
        predictions = {"baseline_department_historical_mean": test["department"].map(means).to_numpy(float)}
        for name, predictors in models.items():
            predictions[name], details[f"{scope}_{season}_{name}"] = fit_predict(predictors, train, test)
        for name, values in predictions.items():
            for record, value in zip(test.to_dict("records"), values, strict=True):
                rows.append({"scope": scope, "held_out_season": season, "model": name, "department": record["department"],
                             "observed_detections": int(record["detections"]), "predicted_detections": float(value)})
    frame = pd.DataFrame(rows)

    original = pd.read_csv(ORIGINAL_ROWS)
    key = ["scope", "held_out_season", "model", "department"]
    check = frame.merge(original[key + ["predicted_detections"]], on=key, suffixes=("", "_original"))
    check = check[check["model"].isin(["baseline_department_historical_mean", "nb_primary_may_july_agricultural"])]
    if len(check) != 2 * 120:
        raise RuntimeError(f"El control de reproducción no encontró las 240 filas, encontró {len(check)}")
    reproduction = float((check["predicted_detections"] - check["predicted_detections_original"]).abs().max())
    if reproduction > 1e-6:
        raise RuntimeError(f"No se reproducen las predicciones originales, diferencia {reproduction}")

    historical_rows = frame[frame["scope"] == "historical_leave_one_season_out"]
    final_rows = frame[frame["scope"] == "final_2026_holdout"]
    performance = {}
    for name in frame["model"].unique():
        h = historical_rows[historical_rows["model"] == name]
        f = final_rows[final_rows["model"] == name]
        tolima_h = h[h["department"] == "Tolima"]
        tolima_f = f[f["department"] == "Tolima"].iloc[0]
        signed_hist = log_error(tolima_h["observed_detections"], tolima_h["predicted_detections"])
        signed_2026 = float(log_error([tolima_f["observed_detections"]], [tolima_f["predicted_detections"]])[0])
        performance[name] = {
            "historical_pooled": summary(h["observed_detections"].to_numpy(float), h["predicted_detections"].to_numpy(float)),
            "historical_tolima": summary(tolima_h["observed_detections"].to_numpy(float), tolima_h["predicted_detections"].to_numpy(float)),
            "final_2026_all_departments": summary(f["observed_detections"].to_numpy(float), f["predicted_detections"].to_numpy(float)),
            "final_2026_total_observed": int(f["observed_detections"].sum()),
            "final_2026_total_predicted": round(float(f["predicted_detections"].sum()), 3),
            "tolima_2026_observed": int(tolima_f["observed_detections"]),
            "tolima_2026_predicted": round(float(tolima_f["predicted_detections"]), 3),
            "tolima_2026_observed_to_predicted": round((tolima_f["observed_detections"] + 0.5) / (tolima_f["predicted_detections"] + 0.5), 4),
            "tolima_2026_signed_log_error": round(signed_2026, 4),
            "tolima_2026_error_rank_among_15_ascending": int(np.sum(signed_hist < signed_2026) + 1),
            "tolima_historical_signed_log_error_max": round(float(signed_hist.max()), 4),
        }

    primary_error = performance["nb_primary_may_july_agricultural"]["historical_pooled"]["mean_absolute_log_count_error"]
    baseline_error = performance["baseline_department_historical_mean"]["historical_pooled"]["mean_absolute_log_count_error"]
    improvement = {
        name: performance[name]["historical_pooled"]["mean_absolute_log_count_error"] < min(primary_error, baseline_error)
        for name in performance if "enso" in name
    }

    rng = np.random.default_rng(SEED)
    association = {}
    for index in INDICES:
        records, p_values = [], []
        for department, group in historical.groupby("department"):
            x = group.sort_values("season")[index].to_numpy(float)
            y = group.sort_values("season")["detections"].to_numpy(float)
            rho = float(stats.spearmanr(x, y)[0])
            # Spearman is Pearson on ranks, so permuting the rank vector gives the same null.
            rx = stats.rankdata(x)
            ry = stats.rankdata(y)
            shuffled = rng.permuted(np.tile(ry, (PERMUTATIONS, 1)), axis=1)
            cx = rx - rx.mean()
            cy = shuffled - shuffled.mean(axis=1, keepdims=True)
            null = (cy @ cx) / (np.sqrt((cy ** 2).sum(axis=1)) * np.sqrt((cx ** 2).sum()))
            p = float((np.sum(np.abs(null) >= abs(rho) - 1e-12) + 1) / (PERMUTATIONS + 1))
            records.append({"department": department, "spearman_rho": round(rho, 4), "permutation_p": round(p, 6)})
            p_values.append(p)
        for record, q in zip(records, benjamini_hochberg(p_values)):
            record["bh_q"] = round(q, 6)
        association[index] = records

    collinearity = {
        index: {
            "pooled_pearson_with_may_july_rain": round(float(historical[[index, RAIN]].corr().iloc[0, 1]), 4),
            "by_department": {d: round(float(g[[index, RAIN]].corr().iloc[0, 1]), 4) for d, g in historical.groupby("department")},
        }
        for index in INDICES
    }

    ROWS_OUT.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(ROWS_OUT, index=False)
    final_coefficients = {name: details[f"final_2026_holdout_{TARGET}_{name}"] for name in models}
    audit = {
        "script": Path(__file__).name,
        "plan": str(PLAN.relative_to(ROOT)).replace("\\", "/"),
        "plan_sha256": sha256(PLAN),
        "status": "exploratorio y posterior",
        "reproduction_max_abs_difference": reproduction,
        "performance": performance,
        "improvement_rule_met": improvement,
        "final_fit_2012_2025": final_coefficients,
        "spearman_by_department_2012_2025": association,
        "collinearity_with_may_july_rain": collinearity,
        "inputs": [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(p)} for p in (PANEL, ENSO, ORIGINAL_ROWS)],
        "outputs": [str(ROWS_OUT.relative_to(ROOT)).replace("\\", "/")],
        "seed": SEED,
        "permutations": PERMUTATIONS,
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    brief = {name: {"pooled_log_error": round(v["historical_pooled"]["mean_absolute_log_count_error"], 4),
                    "tolima_hist_log_error": round(v["historical_tolima"]["mean_absolute_log_count_error"], 4),
                    "2026_all_log_error": round(v["final_2026_all_departments"]["mean_absolute_log_count_error"], 4),
                    "2026_total_pred": v["final_2026_total_predicted"],
                    "tolima_2026_pred": v["tolima_2026_predicted"],
                    "tolima_2026_rank": v["tolima_2026_error_rank_among_15_ascending"]} for name, v in performance.items()}
    print(json.dumps({"reproduction": reproduction, "models": brief, "improvement_rule_met": improvement,
                      "coefficients": {k: v["scaled_coefficients"] for k, v in final_coefficients.items()},
                      "association": association, "collinearity_pooled": {k: v["pooled_pearson_with_may_july_rain"] for k, v in collinearity.items()}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
