"""Evalúa el desacople entre actividad de fuego y peligro atmosférico en Tolima.

Sigue el diseño fijado el 9 de septiembre de 2026 en la revisión crítica del
estudio, antes de ejecutar el ajuste. El ajuste usa solo las
temporadas de referencia. La temporada evaluada no participa en la estimación.

Una exposición atmosférica a la vez, dado que hay catorce temporadas de
referencia. Se reporta el residuo fuera de muestra de 2026 junto a la
distribución completa de residuos fuera de muestra de las temporadas de
referencia, obtenida dejando una temporada fuera cada vez.

No se reportan valores p. La inferencia es descriptiva y predictiva fuera de
muestra, nunca causal.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
ACTIVITY = ROOT / "data" / "processed" / "regional_fire_activity_department_season_v1.csv"
DANGER = ROOT / "data" / "processed" / "era5_land_august_season_danger_1991_2026_v1.csv"
OUTPUT = ROOT / "data" / "processed" / "tolima_activity_danger_decoupling_v1.csv"
AUDIT = ROOT / "results" / "primary" / "audit_activity_danger_decoupling_v1.json"
TARGET_SEASON = 2026
DEPARTMENT = "Tolima"
EXPOSURES = ("vpd_daily_max_mean_kpa", "vpd_afternoon_mean_kpa", "vpd_daily_mean_mean_kpa")
RESPONSES = ("detections", "cells_1km_with_detection")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def fit(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    slope, intercept = np.polyfit(x, y, 1)
    return float(intercept), float(slope)


def main() -> None:
    activity = {int(row["season"]): row for row in read_csv(ACTIVITY) if row["department"] == DEPARTMENT}
    danger = {int(row["season"]): row for row in read_csv(DANGER)}

    seasons = sorted(set(activity) & set(danger))
    reference = [season for season in seasons if season != TARGET_SEASON]
    if TARGET_SEASON not in seasons or len(reference) < 10:
        raise RuntimeError("Cobertura insuficiente para evaluar el desacople")

    results = []
    for response in RESPONSES:
        for exposure in EXPOSURES:
            x_reference = np.asarray([float(danger[season][exposure]) for season in reference])
            y_reference = np.log(np.asarray([float(activity[season][response]) for season in reference]))
            x_target = float(danger[TARGET_SEASON][exposure])
            y_target = float(np.log(float(activity[TARGET_SEASON][response])))

            intercept, slope = fit(x_reference, y_reference)
            fitted = intercept + slope * x_reference
            residuals = y_reference - fitted
            correlation = float(np.corrcoef(x_reference, y_reference)[0, 1])

            loo_residuals = []
            for position, season in enumerate(reference):
                mask = np.ones(len(reference), dtype=bool)
                mask[position] = False
                sub_intercept, sub_slope = fit(x_reference[mask], y_reference[mask])
                loo_residuals.append(float(y_reference[position] - (sub_intercept + sub_slope * x_reference[position])))
            loo = np.asarray(loo_residuals)

            predicted_target = intercept + slope * x_target
            residual_target = y_target - predicted_target
            exceeded = int(np.count_nonzero(np.abs(loo) >= abs(residual_target)))

            results.append(
                {
                    "response": response,
                    "exposure": exposure,
                    "reference_seasons": len(reference),
                    "pearson_r_reference": round(correlation, 4),
                    "r_squared_reference": round(correlation ** 2, 4),
                    "slope_per_kpa": round(slope, 4),
                    "observed_2026": round(float(activity[TARGET_SEASON][response]), 1),
                    "predicted_2026": round(float(np.exp(predicted_target)), 1),
                    "ratio_observed_to_predicted": round(float(np.exp(residual_target)), 3),
                    "residual_log_units": round(float(residual_target), 4),
                    "reference_residual_sd_in_sample": round(float(np.std(residuals, ddof=2)), 4),
                    "leave_one_out_residual_sd": round(float(np.std(loo, ddof=1)), 4),
                    "reference_seasons_with_larger_absolute_loo_residual": exceeded,
                    "standardised_residual_vs_loo": round(float(residual_target / np.std(loo, ddof=1)), 3),
                }
            )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)

    audit = {
        "script": Path(__file__).name,
        "executed": "2026-09-09",
        "department": DEPARTMENT,
        "target_season": TARGET_SEASON,
        "reference_seasons": reference,
        "response_transform": "logaritmo natural del conteo",
        "estimation_rule": "el ajuste usa solo temporadas de referencia y una exposición a la vez",
        "inputs": [
            {"path": str(ACTIVITY.relative_to(ROOT)), "sha256": sha256(ACTIVITY)},
            {"path": str(DANGER.relative_to(ROOT)), "sha256": sha256(DANGER)},
        ],
        "results": results,
        "processing_level_warning": "La temporada 2026 usa tiempo casi real y las de referencia usan procesamiento estándar. El desplazamiento entre niveles no está cuantificado y solo podrá estimarse cuando se publique el estándar de agosto de 2026.",
        "interpretation_limits": [
            "Catorce temporadas de referencia y una sola exposición por modelo. No se afirma un modelo predictivo validado.",
            "El residuo describe cuánto se aparta la actividad observada de la relación histórica con el peligro atmosférico. No identifica una causa.",
            "Una detección no es un incendio ni área quemada.",
        ],
        "outputs": [str(OUTPUT.relative_to(ROOT))],
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'desenlace':<26}{'exposicion':<28}{'r2':>7}{'obs':>7}{'pred':>8}{'razon':>8}{'z_loo':>8}{'peores_ref':>11}")
    for row in results:
        print(f"{row['response']:<26}{row['exposure']:<28}{row['r_squared_reference']:>7}"
              f"{row['observed_2026']:>7.0f}{row['predicted_2026']:>8.0f}{row['ratio_observed_to_predicted']:>8}"
              f"{row['standardised_residual_vs_loo']:>8}{row['reference_seasons_with_larger_absolute_loo_residual']:>11}")


if __name__ == "__main__":
    main()
