"""Selección anidada de modelos, ventanas de precipitación y estabilidad del coeficiente del ONI.

Sigue las partes F, G e I de `configs/review_robustness_plan_v2.json`, congeladas
antes de ejecutar. Usa la misma familia, escalado y controles de convergencia del
script 175, reimplementados aquí sin importar statsmodels.api, que la política de
control de aplicaciones del equipo bloquea. Retrospectivo y posterior.
"""

from __future__ import annotations

import hashlib
import json
import math
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
from statsmodels.tools.sm_exceptions import ConvergenceWarning


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "review_robustness_plan_v2.json"
PANEL = ROOT / "data" / "processed" / "regional_preseason_prediction_panel_v1.csv"
CHIRPS = ROOT / "data" / "processed" / "chirps_regional_antecedent_precipitation_v1.csv"
ENSO = ROOT / "data" / "interim" / "enso_indices_2012_2026_v1.csv"
AUDIT175 = ROOT / "results" / "exploratory" / "audit_regional_enso_prediction_v1.json"
ROWS175 = ROOT / "results" / "exploratory" / "regional_enso_prediction_rows_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_review_nested_selection_rain_windows_v1.json"
HISTORICAL = list(range(2012, 2026))
TARGET = 2026
RAIN, COVER = "chirps_may_july_mean_mm", "mapbiomas_agricultural_fraction_lagged"
CANDIDATES = {
    "nb_primary_may_july_agricultural": [RAIN, COVER],
    "nb_enso_only_oni_mjj": ["oni_mjj"],
    "nb_enso_only_oni_amj": ["oni_amj"],
    "nb_enso_only_roni_mjj": ["roni_mjj"],
    "nb_enso_plus_primary_oni_mjj": ["oni_mjj", RAIN, COVER],
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def fit_predict(predictors, train, test):
    train, test = train.copy(), test.copy()
    for c in predictors:
        med = float(train[c].median()); q1, q3 = train[c].quantile([0.25, 0.75]); iqr = float(q3 - q1)
        if not math.isfinite(iqr) or iqr <= 0:
            raise RuntimeError(f"Rango intercuartílico no positivo para {c}")
        train[f"scaled_{c}"] = (train[c] - med) / iqr
        test[f"scaled_{c}"] = (test[c] - med) / iqr
    formula = "detections ~ " + " + ".join([f"scaled_{p}" for p in predictors] + ["C(department)"])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = smf.negativebinomial(formula, data=train).fit(disp=False, maxiter=500)
    if not result.mle_retvals.get("converged", False) or any(issubclass(w.category, ConvergenceWarning) for w in caught):
        raise RuntimeError(f"Sin convergencia, {formula}")
    return np.asarray(result.predict(test), dtype=float)


def log_error(obs, pred):
    return np.abs(np.log((np.asarray(obs, float) + 0.5) / (np.asarray(pred, float) + 0.5)))


def loso_error(predictors, data, seasons):
    errors = []
    for s in seasons:
        train, test = data[data["season"].isin([x for x in seasons if x != s])], data[data["season"] == s]
        errors.extend(log_error(test["detections"], fit_predict(predictors, train, test)))
    return float(np.mean(errors))


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribe")
    panel = pd.read_csv(PANEL).merge(pd.read_csv(ENSO), on="season", validate="many_to_one")
    chirps = pd.read_csv(CHIRPS)
    chirps["chirps_june_july_mm"] = chirps["chirps_june_mean_mm"] + chirps["chirps_july_mean_mm"]
    panel = panel.merge(chirps[["department", "season", "chirps_june_july_mm"]], on=["department", "season"], validate="one_to_one")
    hist, target = panel[panel["season"].isin(HISTORICAL)], panel[panel["season"] == TARGET]

    # Control de reproducción frente al script 175.
    rows175 = pd.read_csv(ROWS175)
    for name, preds in CANDIDATES.items():
        p = fit_predict(preds, hist, target)
        stored = rows175[(rows175["scope"] == "final_2026_holdout") & (rows175["model"] == name)].set_index("department")["predicted_detections"]
        if np.max(np.abs(p - stored.loc[target["department"]].to_numpy())) > 1e-6:
            raise RuntimeError(f"No se reproduce el script 175 para {name}")

    # F. Selección anidada.
    outer, chosen = [], {}
    for s in HISTORICAL:
        inner_seasons = [x for x in HISTORICAL if x != s]
        inner = {name: loso_error(preds, hist, inner_seasons) for name, preds in CANDIDATES.items()}
        best = min(inner, key=inner.get)
        chosen[s] = best
        train, test = hist[hist["season"] != s], hist[hist["season"] == s]
        outer.extend(log_error(test["detections"], fit_predict(CANDIDATES[best], train, test)))
    full = {name: loso_error(preds, hist, HISTORICAL) for name, preds in CANDIDATES.items()}
    best_2026 = min(full, key=full.get)
    pred_2026 = fit_predict(CANDIDATES[best_2026], hist, target)
    baseline_errors = []
    for s in HISTORICAL:
        train, test = hist[hist["season"] != s], hist[hist["season"] == s]
        means = train.groupby("department")["detections"].mean()
        baseline_errors.extend(log_error(test["detections"], test["department"].map(means)))
    nested = {
        "outer_mean_absolute_log_error_selection_procedure": round(float(np.mean(outer)), 4),
        "fixed_primary_loso_error": round(full["nb_primary_may_july_agricultural"], 4),
        "baseline_loso_error": round(float(np.mean(baseline_errors)), 4),
        "choice_frequency": {k: list(chosen.values()).count(k) for k in CANDIDATES},
        "full_loso_errors": {k: round(v, 4) for k, v in full.items()},
        "selected_for_2026": best_2026,
        "selected_tolima_2026_prediction": round(float(pred_2026[list(target["department"]).index("Tolima")]), 1),
    }

    # G. Ventanas de precipitación.
    windows = {"july": "chirps_july_mean_mm", "june_july": "chirps_june_july_mm", "may_july": RAIN}
    regional, tolima_series = {}, {}
    tol = panel[panel["department"] == "Tolima"].set_index("season")
    logd = np.log(tol.loc[HISTORICAL, "detections"].astype(float).to_numpy())
    for label, col in windows.items():
        p = fit_predict([col, COVER], hist, target)
        regional[label] = {"loso_error": round(loso_error([col, COVER], hist, HISTORICAL), 4),
                           "tolima_2026_prediction": round(float(p[list(target["department"]).index("Tolima")]), 1)}
        x = tol.loc[HISTORICAL, col].to_numpy(float)
        loo = []
        for i in range(len(x)):
            m = np.arange(len(x)) != i
            b, a = np.polyfit(x[m], logd[m], 1)
            loo.append(abs(a + b * x[i] - logd[i]))
        b, a = np.polyfit(x, logd, 1)
        tolima_series[label] = {"loo_error": round(float(np.mean(loo)), 4), "prediction_2026": round(float(np.exp(a + b * tol.loc[TARGET, col])), 1),
                                "value_2026": round(float(tol.loc[TARGET, col]), 1)}

    # I. Estabilidad del coeficiente del ONI.
    details = json.loads(AUDIT175.read_text(encoding="utf-8"))["final_fit_2012_2025"]
    audit175 = json.loads(AUDIT175.read_text(encoding="utf-8"))
    coefs = []
    for key, value in audit175.get("fit_details", {}).items():
        if key.startswith("historical_leave_one_season_out") and key.endswith("nb_enso_only_oni_mjj"):
            coefs.append(value["scaled_coefficients"]["oni_mjj"])
    if not coefs:
        for s in HISTORICAL:
            train = hist[hist["season"] != s].copy()
            med = float(train["oni_mjj"].median()); q1, q3 = train["oni_mjj"].quantile([0.25, 0.75])
            train["scaled_oni_mjj"] = (train["oni_mjj"] - med) / float(q3 - q1)
            r = smf.negativebinomial("detections ~ scaled_oni_mjj + C(department)", data=train).fit(disp=False, maxiter=500)
            coefs.append(float(r.params["scaled_oni_mjj"]))
    stability = {"folds": len(coefs), "min": round(min(coefs), 4), "max": round(max(coefs), 4), "all_positive": all(c > 0 for c in coefs),
                 "full_fit": round(details["nb_enso_only_oni_mjj"]["scaled_coefficients"]["oni_mjj"], 4)}

    audit = {"script": Path(__file__).name, "plan_sha256": sha256(PLAN), "status": "retrospectivo y posterior",
             "F_nested_selection": nested, "G_precipitation_windows": {"regional": regional, "tolima_series": tolima_series},
             "I_oni_coefficient_stability": stability,
             "inputs": [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(p)} for p in (PANEL, CHIRPS, ENSO, AUDIT175, ROWS175)]}
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: audit[k] for k in ("F_nested_selection", "G_precipitation_windows", "I_oni_coefficient_stability")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
