"""Intervalos de predicción de Tolima 2026 y comparación de demanda concurrente y antecedente.

Sigue `configs/review_robustness_plan_v1.json`, partes D y E, congelado antes de
ejecutar. Parte D: simulación paramétrica desde el ajuste final de 2012 a 2025 de
los modelos binomiales negativos del script 175. Parte E: regresiones del logaritmo
de las detecciones de Tolima con validación dejando una temporada fuera.
Descriptivo, retrospectivo y posterior.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "review_robustness_plan_v1.json"
PANEL = ROOT / "data" / "processed" / "regional_preseason_prediction_panel_v1.csv"
ENSO = ROOT / "data" / "interim" / "enso_indices_2012_2026_v1.csv"
FIRE_VPD = ROOT / "data" / "processed" / "tolima_fire_location_demand_season_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_review_prediction_intervals_specifications_v1.json"
HISTORICAL = list(range(2012, 2026))
TARGET = 2026
RAIN = "chirps_may_july_mean_mm"
COVER = "mapbiomas_agricultural_fraction_lagged"
MODELS = {"nb_primary_may_july_agricultural": [RAIN, COVER], "nb_enso_only_oni_mjj": ["oni_mjj"], "nb_enso_only_oni_amj": ["oni_amj"]}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def scale(train: pd.DataFrame, test: pd.DataFrame, columns: list[str]):
    """Misma regla del script 175, mediana y rango intercuartílico del entrenamiento."""
    train, test = train.copy(), test.copy()
    for column in columns:
        median = float(train[column].median())
        q1, q3 = train[column].quantile([0.25, 0.75])
        train[f"scaled_{column}"] = (train[column] - median) / float(q3 - q1)
        test[f"scaled_{column}"] = (test[column] - median) / float(q3 - q1)
    return train, test


def part_d(panel: pd.DataFrame, plan: dict) -> dict:
    rng = np.random.default_rng(plan["D_prediction_intervals"]["seed"])
    draws = plan["D_prediction_intervals"]["draws"]
    historical = panel[panel["season"].isin(HISTORICAL)].copy()
    target = panel[panel["season"] == TARGET].copy()
    out = {}
    for name, predictors in MODELS.items():
        train, test = scale(historical, target, predictors)
        formula = "detections ~ " + " + ".join([f"scaled_{p}" for p in predictors] + ["C(department)"])
        result = smf.negativebinomial(formula, data=train).fit(disp=False, maxiter=500)
        point = np.asarray(result.predict(test), dtype=float)
        tolima = test[test["department"] == "Tolima"]
        params, cov = result.params, result.cov_params()
        exog_names = [n for n in params.index if n != "alpha"]
        x_target = np.asarray(smf.negativebinomial(formula, data=pd.concat([train, tolima])).exog[-1], dtype=float)
        if [n for n in result.model.exog_names if n != "alpha"] != exog_names:
            raise RuntimeError("Orden de coeficientes inesperado")
        simulated = rng.multivariate_normal(params.values, cov.values, size=draws)
        beta, alpha = simulated[:, :-1], simulated[:, -1]
        alpha = np.where(alpha > 1e-6, alpha, 1e-6)
        mu = np.exp(beta @ x_target)
        size = 1.0 / alpha
        counts = rng.negative_binomial(size, size / (size + mu))
        tolima_point = float(point[list(target["department"]).index("Tolima")])
        mu_ci = np.quantile(mu, [0.05, 0.95])
        y_pi = np.quantile(counts, [0.05, 0.95])
        stored = pd.read_csv(ROOT / "results" / "exploratory" / "regional_enso_prediction_rows_v1.csv")
        stored = stored[(stored["scope"] == "final_2026_holdout") & (stored["model"] == name) & (stored["department"] == "Tolima")]
        if abs(float(stored["predicted_detections"].iloc[0]) - tolima_point) > 1e-6:
            raise RuntimeError(f"No se reproduce la predicción del script 175 para {name}")
        out[name] = {"point_prediction": round(tolima_point, 3), "mean_90_interval": [round(float(v), 1) for v in mu_ci],
                     "count_90_interval": [int(v) for v in y_pi], "observed_685_inside_count_interval": bool(y_pi[0] <= 685 <= y_pi[1]),
                     "alpha": round(float(params["alpha"]), 4)}
    return out


def part_e(panel: pd.DataFrame) -> dict:
    tolima = panel[panel["department"] == "Tolima"].set_index("season")
    fire = pd.read_csv(FIRE_VPD).set_index("season")
    data = pd.DataFrame({
        "log_detections": np.log(tolima["detections"].astype(float)),
        "vpd_fire": fire.loc[tolima.index, "vpd_daily_max_mean_kpa_fire_prone_reference"],
        "oni_mjj": tolima["oni_mjj"],
        "rain": tolima[RAIN],
    })
    specs = {"null": [], "concurrent_vpd_fire_cells": ["vpd_fire"], "antecedent_oni_mjj": ["oni_mjj"], "antecedent_rain": ["rain"],
             "concurrent_plus_oni": ["vpd_fire", "oni_mjj"], "concurrent_plus_rain": ["vpd_fire", "rain"]}
    hist = data.loc[HISTORICAL]
    out = {}
    for name, cols in specs.items():
        formula = "log_detections ~ " + (" + ".join(cols) if cols else "1")
        errors = []
        for s in HISTORICAL:
            fit = smf.ols(formula, data=hist.drop(index=s)).fit()
            errors.append(abs(float(fit.predict(hist.loc[[s]]).iloc[0]) - hist.loc[s, "log_detections"]))
        final = smf.ols(formula, data=hist).fit()
        pred_2026 = float(np.exp(final.predict(data.loc[[TARGET]]).iloc[0]))
        out[name] = {"loo_mean_absolute_log_error": round(float(np.mean(errors)), 4), "prediction_2026": round(pred_2026, 1),
                     "observed_2026": 685, "coefficients": {k: round(float(v), 4) for k, v in final.params.items()},
                     "r2_2012_2025": round(float(final.rsquared), 4)}
    out["values_2026"] = {k: round(float(v), 4) for k, v in data.loc[TARGET].items()}
    out["reference_range"] = {k: [round(float(hist[k].min()), 4), round(float(hist[k].max()), 4)] for k in ("vpd_fire", "oni_mjj", "rain")}
    return out


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribe")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    panel = pd.read_csv(PANEL).merge(pd.read_csv(ENSO), on="season", how="left", validate="many_to_one")
    audit = {
        "script": Path(__file__).name,
        "plan_sha256": sha256(PLAN),
        "status": "descriptivo, retrospectivo y posterior",
        "D_prediction_intervals_tolima_2026": part_d(panel, plan),
        "E_tolima_concurrent_vs_antecedent": part_e(panel),
        "inputs": [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(p)} for p in (PANEL, ENSO, FIRE_VPD)],
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
