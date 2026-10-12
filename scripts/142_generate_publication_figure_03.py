"""Create a Spanish labelled, 600 dpi coefficient figure from executed primary models."""
from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "results" / "primary" / "primary_models_v1.csv"
OUTPUT = ROOT / "submission_figures" / "Figure_03_primary_coefficients.png"


def numeric(row, key):
    return float(row[key])


def main():
    with SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    outcome_labels = {
        "log_geometry_area_ha": "Logaritmo del área geométrica",
        "log1p_detection_interval_hours": "Logaritmo de uno más el intervalo de detección",
    }
    exposure_labels = {
        "vpd_mean_first_24h_kpa": "Déficit de presión de vapor",
        "wind_mean_first_24h_ms": "Velocidad del viento",
    }
    selected = [row for row in rows if row["term"] in exposure_labels]
    labels = [
        outcome_labels[row["outcome"]] + "\n" + exposure_labels[row["term"]]
        for row in selected
    ]
    estimate = np.asarray([numeric(row, "coefficient") for row in selected])
    lower = np.asarray([numeric(row, "confidence_interval_95_lower") for row in selected])
    upper = np.asarray([numeric(row, "confidence_interval_95_upper") for row in selected])
    position = np.arange(len(selected))
    figure, axis = plt.subplots(figsize=(9.2, 4.8))
    axis.errorbar(
        estimate,
        position,
        xerr=np.vstack((estimate - lower, upper - estimate)),
        fmt="o",
        color="#8d253d",
        ecolor="#8d253d",
        capsize=3,
    )
    axis.axvline(0, color="#333333", linewidth=0.8)
    axis.set(
        yticks=position,
        yticklabels=labels,
        xlabel="Coeficiente ajustado por rango intercuartílico\nIntervalo de confianza de 95 por ciento",
        title="Modelos principales con errores estándar HC3",
    )
    axis.invert_yaxis()
    figure.tight_layout()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(OUTPUT, dpi=600, bbox_inches="tight")
    plt.close(figure)
    dpi = Image.open(OUTPUT).info.get("dpi", (0, 0))
    if min(dpi) < 599:
        raise RuntimeError(f"La figura no alcanzó 600 dpi, valor observado {dpi}")
    print(f"Created {OUTPUT}")


if __name__ == "__main__":
    main()
