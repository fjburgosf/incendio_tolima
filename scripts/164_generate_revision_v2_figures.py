"""Generate high resolution figures from executed regional and spatial results."""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parents[1] / ".matplotlib_cache"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PREDICTIONS = ROOT / "results" / "primary" / "regional_preseason_prediction_rows_v1.csv"
SPATIAL = ROOT / "results" / "primary" / "tolima_spatial_case_control_model_v1.csv"
OUTPUT_DIR = ROOT / "figures" / "revision_v2"
AUDIT = ROOT / "results" / "primary" / "audit_revision_v2_figures_v1.json"


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("Figure audit exists and must not be overwritten")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    prediction = pd.read_csv(PREDICTIONS)
    spatial = pd.read_csv(SPATIAL)
    final = prediction.loc[(prediction["scope"] == "final_2026_holdout") & (prediction["department"] == "Tolima")].copy()
    values = {
        row["model"]: float(row["predicted_detections"])
        for _, row in final.iterrows()
        if row["model"] in {"baseline_department_historical_mean", "nb_primary_may_july_agricultural"}
    }
    observed = float(final["observed_detections"].iloc[0])
    historic = prediction.loc[
        (prediction["scope"] == "historical_leave_one_season_out")
        & (prediction["department"] == "Tolima")
        & (prediction["model"] == "nb_primary_may_july_agricultural")
    ].copy()
    historic_errors = np.abs(np.log((historic["observed_detections"] + 0.5) / (historic["predicted_detections"] + 0.5)))
    target_error = abs(np.log((observed + 0.5) / (values["nb_primary_may_july_agricultural"] + 0.5)))

    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.3), constrained_layout=True)
    labels = ["Historical baseline", "Preseason model", "Observed"]
    heights = [values["baseline_department_historical_mean"], values["nb_primary_may_july_agricultural"], observed]
    colors = ["#9aa0a6", "#3182bd", "#e6550d"]
    bars = axes[0].bar(labels, heights, color=colors, width=0.66)
    axes[0].set_ylabel("VIIRS Suomi NPP detections")
    axes[0].set_title("Tolima, August 2026")
    axes[0].set_ylim(0, max(heights) * 1.19)
    for bar, value in zip(bars, heights):
        axes[0].text(bar.get_x() + bar.get_width() / 2, value + 14, f"{value:.1f}", ha="center", va="bottom", fontsize=9)
    axes[0].spines[["top", "right"]].set_visible(False)

    axes[1].hist(historic_errors, bins=7, color="#9ecae1", edgecolor="white")
    axes[1].axvline(target_error, color="#e6550d", linewidth=2)
    axes[1].text(target_error, axes[1].get_ylim()[1] * 0.92, "2026 error", color="#e6550d", ha="left", va="top", fontsize=9)
    axes[1].set_xlabel("Absolute logarithmic error")
    axes[1].set_ylabel("Held out historical seasons")
    axes[1].set_title("Tolima, validation 2012 to 2025")
    axes[1].spines[["top", "right"]].set_visible(False)
    path1 = OUTPUT_DIR / "Figure_3_regional_preseason_validation.png"
    fig.savefig(path1, dpi=600, bbox_inches="tight")
    plt.close(fig)

    keep = spatial.loc[spatial["term"].isin(["slope_degrees_per_iqr", "upra_frontier_fraction_per_iqr"])].copy()
    names = {
        "primary_linear": "Linear elevation",
        "sensitivity_elevation_quartiles": "Elevation quartiles",
        "sensitivity_one_case_per_five_kilometre_block": "One case per 5 km block",
    }
    terms = {"slope_degrees_per_iqr": "Slope", "upra_frontier_fraction_per_iqr": "UPRA frontier"}
    fig, ax = plt.subplots(figsize=(8.4, 4.7), constrained_layout=True)
    positions = []
    labels = []
    colors = {"slope_degrees_per_iqr": "#3182bd", "upra_frontier_fraction_per_iqr": "#756bb1"}
    for term_index, term in enumerate(["slope_degrees_per_iqr", "upra_frontier_fraction_per_iqr"]):
        subset = keep.loc[keep["term"] == term]
        for model_index, model in enumerate(names):
            row = subset.loc[subset["model"] == model].iloc[0]
            y = term_index * 4 + model_index
            positions.append(y)
            labels.append(f"{terms[term]}  {names[model]}")
            ax.errorbar(
                row["odds_ratio"], y,
                xerr=[[row["odds_ratio"] - row["ci95_lower_odds_ratio"]], [row["ci95_upper_odds_ratio"] - row["odds_ratio"]]],
                fmt="o", color=colors[term], capsize=3, markersize=5,
            )
    ax.axvline(1, color="#555555", linewidth=1, linestyle="--")
    ax.set_xscale("log")
    ax.set_yticks(positions, labels)
    ax.set_xlabel("Odds ratio, logarithmic scale")
    ax.set_title("Spatial associations within municipalities")
    ax.spines[["top", "right"]].set_visible(False)
    path2 = OUTPUT_DIR / "Figure_4_spatial_association_sensitivities.png"
    fig.savefig(path2, dpi=600, bbox_inches="tight")
    plt.close(fig)

    audit = {
        "script": Path(__file__).name,
        "figures": [],
        "interpretation_limits": [
            "The first figure shows predictions of satellite detections, not fire occurrence or burned area.",
            "The second figure shows conditional associations within matched municipalities, not causal effects.",
            "The UPRA association changes under predeclared sensitivities and must not be treated as robust.",
        ],
    }
    for path in [path1, path2]:
        image = Image.open(path)
        audit["figures"].append({"path": str(path.relative_to(ROOT)).replace("\\", "/"), "pixels": image.size, "dpi": image.info.get("dpi")})
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
