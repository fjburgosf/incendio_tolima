"""Genera la figura del análisis diario entre demanda atmosférica y actividad.

Acompaña al script 168. El panel izquierdo muestra cada día de agosto entre 2012 y
2026 con su máximo diario de déficit de presión de vapor y su conteo de
detecciones. El panel derecho muestra qué proporción de las detecciones de cada
temporada ocurrió en días por encima del percentil 90 de referencia.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DAILY = ROOT / "data" / "processed" / "tolima_daily_vpd_activity_v1.csv"
OUTPUT = ROOT / "figures" / "revision_v2" / "Figure_5_daily_demand_and_activity.png"
AUDIT = ROOT / "results" / "exploratory" / "audit_daily_vpd_figure_v1.json"
TARGET = 2026


def main() -> None:
    with DAILY.open(encoding="utf-8-sig", newline="") as handle:
        rows = [
            {
                "season": int(row["season"]),
                "vpd": float(row["vpd_daily_max_kpa"]),
                "detections": int(row["detections"]),
            }
            for row in csv.DictReader(handle)
        ]

    reference = [row for row in rows if row["season"] != TARGET]
    threshold = float(np.percentile([row["vpd"] for row in reference], 90))

    figure, axes = plt.subplots(1, 2, figsize=(11.0, 4.2))

    other = [row for row in rows if row["season"] != TARGET]
    target = [row for row in rows if row["season"] == TARGET]
    axes[0].scatter(
        [row["vpd"] for row in other],
        [row["detections"] for row in other],
        s=22, color="#3182bd", alpha=0.55, linewidths=0, label="2012 to 2025",
    )
    axes[0].scatter(
        [row["vpd"] for row in target],
        [row["detections"] for row in target],
        s=46, color="#e6550d", zorder=3, label="2026",
    )
    axes[0].axvline(threshold, color="#636363", linestyle="--", linewidth=1.1)
    axes[0].text(
        threshold, axes[0].get_ylim()[1] * 0.95,
        f" Reference 90th percentile, {threshold:.2f} kPa",
        fontsize=8, va="top", ha="left", color="#636363",
    )
    axes[0].set_xlabel("Daily maximum VPD in kPa")
    axes[0].set_ylabel("Daily VIIRS Suomi NPP detections")
    axes[0].set_title("August days in Tolima, 2012 to 2026")
    axes[0].legend(frameon=False, fontsize=8)

    shares = defaultdict(lambda: [0, 0])
    for row in rows:
        shares[row["season"]][1] += row["detections"]
        if row["vpd"] > threshold:
            shares[row["season"]][0] += row["detections"]
    seasons = sorted(shares)
    values = [100.0 * shares[s][0] / shares[s][1] if shares[s][1] else 0.0 for s in seasons]
    colours = ["#e6550d" if s == TARGET else "#9ecae1" for s in seasons]
    axes[1].bar([str(s) for s in seasons], values, color=colours)
    axes[1].set_ylabel("Percentage of seasonal detections")
    axes[1].set_xlabel("August season")
    axes[1].set_title("Detections on days above the reference threshold")
    axes[1].tick_params(axis="x", rotation=90, labelsize=8)

    for axis in axes:
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)

    figure.tight_layout()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(OUTPUT, dpi=600, bbox_inches="tight")
    plt.close(figure)

    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text(
        json.dumps(
            {
                "script": Path(__file__).name,
                "executed": "2026-09-21",
                "status": "exploratorio, acompana al script 168",
                "threshold_kpa": round(threshold, 4),
                "days_plotted": len(rows),
                "share_by_season": {str(s): round(v, 2) for s, v in zip(seasons, values)},
                "output": str(OUTPUT.relative_to(ROOT)),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"creada {OUTPUT.relative_to(ROOT)}")
    print("porcentaje por temporada", {str(s): round(v, 1) for s, v in zip(seasons, values)})


if __name__ == "__main__":
    main()
