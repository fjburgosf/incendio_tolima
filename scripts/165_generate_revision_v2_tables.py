"""Create manuscript candidate tables from executed revision v2 analyses."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "results" / "primary" / "regional_preseason_prediction_summary_v1.csv"
SPATIAL = ROOT / "results" / "primary" / "tolima_spatial_case_control_model_v1.csv"
OUTPUT_DIR = ROOT / "tables" / "revision_v2_final"
AUDIT = ROOT / "results" / "primary" / "audit_revision_v2_tables_v2.json"


def write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def format_p(value: float) -> str:
    return "< 0.0001" if value < 0.0001 else f"{value:.4f}"


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("Table audit exists and must not be overwritten")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = pd.read_csv(SUMMARY)
    spatial = pd.read_csv(SPATIAL)
    order = [
        "baseline_department_historical_mean",
        "nb_primary_may_july_agricultural",
        "nb_sensitivity_july_agricultural",
        "nb_sensitivity_may_july_natural_nonforest",
        "poisson_primary_may_july_agricultural",
    ]
    labels = {
        "baseline_department_historical_mean": "Media histórica por departamento",
        "nb_primary_may_july_agricultural": "Binomial negativo, lluvia mayo a julio y área agropecuaria",
        "nb_sensitivity_july_agricultural": "Sensibilidad, lluvia de julio",
        "nb_sensitivity_may_july_natural_nonforest": "Sensibilidad, vegetación natural no boscosa",
        "poisson_primary_may_july_agricultural": "Sensibilidad, distribución Poisson",
    }
    historical = summary.loc[summary["scope"] == "historical_pooled_leave_one_season_out"].set_index("model")
    final = summary.loc[summary["scope"] == "final_2026_all_departments"].set_index("model")
    lines = [
        "# Tabla 1. Desempeño predictivo temporal de actividad térmica detectada",
        "",
        "| Modelo | MAE histórico | Error logarítmico histórico | MAE 2026 regional | Predicción 2026 regional | Observado 2026 regional |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for model in order:
        h = historical.loc[model]
        f = final.loc[model]
        lines.append(
            f"| {labels[model]} | {h['mae']:.2f} | {h['mean_absolute_log_count_error']:.3f} | {f['mae']:.2f} | {f['total_predicted']:.1f} | {f['total_observed']:.0f} |"
        )
    lines += [
        "",
        "Las temporadas históricas se validan dejando fuera un agosto completo entre 2012 y 2025. La evaluación de 2026 entrena con todas las temporadas estándar anteriores. El desenlace es detección VIIRS Suomi NPP y no número de incendios ni área quemada.",
    ]
    path1 = OUTPUT_DIR / "Table_01_regional_preseason_prediction.md"
    write(path1, "\n".join(lines) + "\n")

    subset = spatial.loc[spatial["term"].isin(["slope_degrees_per_iqr", "upra_frontier_fraction_per_iqr"])].copy()
    model_labels = {
        "primary_linear": "Modelo lineal",
        "sensitivity_elevation_quartiles": "Elevación por cuartiles",
        "sensitivity_one_case_per_five_kilometre_block": "Una celda caso por bloque de 5 km",
    }
    term_labels = {
        "slope_degrees_per_iqr": "Pendiente, por IQR",
        "upra_frontier_fraction_per_iqr": "Fracción de frontera UPRA, por IQR",
    }
    lines = [
        "# Tabla 2. Asociaciones espaciales condicionales dentro de municipios",
        "",
        "| Término | Especificación | Razón de odds | IC 95 por ciento | Valor p Wald | Casos |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for term in ["slope_degrees_per_iqr", "upra_frontier_fraction_per_iqr"]:
        for model in model_labels:
            row = subset.loc[(subset["term"] == term) & (subset["model"] == model)].iloc[0]
            lines.append(
                f"| {term_labels[term]} | {model_labels[model]} | {row['odds_ratio']:.2f} | {row['ci95_lower_odds_ratio']:.2f} a {row['ci95_upper_odds_ratio']:.2f} | {format_p(float(row['p_value_wald']))} | {int(row['cases'])} |"
            )
    lines += [
        "",
        "La muestra principal tiene 390 celdas con detección y 1.170 controles sin detección, apareados dentro de municipio. Las asociaciones no identifican ignición, causalidad, uso observado del suelo ni riesgo absoluto.",
    ]
    path2 = OUTPUT_DIR / "Table_02_spatial_case_control_associations.md"
    write(path2, "\n".join(lines) + "\n")
    payload = {"script": Path(__file__).name, "tables": [str(path1.relative_to(ROOT)).replace("\\", "/"), str(path2.relative_to(ROOT)).replace("\\", "/")]}
    AUDIT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
