"""Generate numbered historical environmental comparison artifacts from audited summaries."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path

os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parents[1] / "logs" / "matplotlib_config"))

import matplotlib.pyplot as plt
import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "historical_environmental_comparison_plan_v1.json"
SUMMARY = ROOT / "data" / "processed" / "historical_environmental_season_summary_v1.csv"
AUDIT_INPUT = ROOT / "results" / "primary" / "audit_historical_environmental_comparison_v1.json"
TABLE = ROOT / "tables" / "Table_04_historical_environmental_comparison.csv"
FIGURE = ROOT / "figures" / "Figure_04_historical_environmental_comparison.png"
AUDIT = ROOT / "results" / "primary" / "audit_historical_environmental_tables_figures_v1.json"
METRICS = [
    ("air_temperature_mean_c", "Temperatura media a 2 m", "grados C"),
    ("vapour_pressure_deficit_mean_kpa", "Déficit de presión de vapor medio", "kPa"),
    ("wind_speed_mean_ms", "Velocidad media del viento a 10 m", "m s menos uno"),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def main() -> None:
    for path in (TABLE, FIGURE, AUDIT):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing numbered output: {path}")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    with SUMMARY.open(encoding="utf-8", newline="") as handle:
        records = list(csv.DictReader(handle))
    selected = [record for record in records if record["metric"] in {metric for metric, _, _ in METRICS} and record["seasonal_summary"] == "mean"]
    expected = len(METRICS) * len(plan["seasons"])
    if len(selected) != expected:
        raise RuntimeError(f"Unexpected historical comparison row count, expected {expected} found {len(selected)}")
    rows = []
    for metric, label, unit in METRICS:
        for year in plan["seasons"]:
            record = next(record for record in selected if record["metric"] == metric and int(record["season"]) == year)
            rows.append(
                {
                    "metric": metric,
                    "metric_label": label,
                    "unit": unit,
                    "season": year,
                    "august_daily_mean": float(record["value"]),
                    "reference_median": record["reference_median"],
                    "reference_minimum": record["reference_minimum"],
                    "reference_maximum": record["reference_maximum"],
                    "difference_from_reference_median": record["difference_from_reference_median"],
                    "target_rank_among_six": record["target_rank_among_six"],
                    "gwis_operational_event_count": record["gwis_operational_event_count_same_season"],
                }
            )
    TABLE.parent.mkdir(parents=True, exist_ok=True)
    with TABLE.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    figure, axes = plt.subplots(1, 3, figsize=(11.5, 3.6), sharex=True)
    seasons = np.asarray(plan["seasons"], dtype=int)
    reference = np.asarray(plan["reference_seasons"], dtype=int)
    for axis, (metric, label, unit) in zip(axes, METRICS, strict=True):
        values = np.asarray([float(next(record for record in selected if record["metric"] == metric and int(record["season"]) == year)["value"]) for year in seasons])
        historical_values = np.asarray([float(next(record for record in selected if record["metric"] == metric and int(record["season"]) == year)["value"]) for year in reference])
        axis.axhspan(float(historical_values.min()), float(historical_values.max()), color="#dce9df", alpha=0.8, label="Rango de referencia" if metric == METRICS[0][0] else None)
        axis.plot(seasons, values, color="#557a95", linewidth=1.2, zorder=2)
        axis.scatter(reference, historical_values, color="#557a95", s=28, zorder=3)
        axis.scatter([2026], [values[-1]], color="#bd3d3a", s=42, zorder=4, label="2026" if metric == METRICS[0][0] else None)
        axis.axhline(float(np.median(historical_values)), color="#496250", linewidth=0.9, linestyle="dotted")
        axis.set_title(label, fontsize=9)
        axis.set_xlabel("Temporada")
        axis.set_ylabel(unit)
        axis.set_xticks(seasons)
        axis.tick_params(axis="x", rotation=45)
    axes[0].legend(frameon=False, fontsize=8, loc="best")
    figure.suptitle("Configuración atmosférica de agosto en temporadas técnicamente comparables", y=1.02, fontsize=11)
    figure.tight_layout()
    figure.savefig(FIGURE, dpi=600, bbox_inches="tight")
    plt.close(figure)
    dpi = list(Image.open(FIGURE).info.get("dpi", (0, 0)))
    if min(dpi) < 599:
        raise RuntimeError(f"Figure DPI check failed: {dpi}")
    output = {
        "plan": {"file": str(PLAN.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(PLAN)},
        "inputs": {"summary": {"file": str(SUMMARY.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(SUMMARY)}, "construction_audit": {"file": str(AUDIT_INPUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(AUDIT_INPUT)}},
        "table": {"file": str(TABLE.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(TABLE), "row_count": len(rows)},
        "figure": {"file": str(FIGURE.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(FIGURE), "dpi": dpi},
        "interpretation_limit": "The shaded range and seasonal ranks are descriptive summaries of five technically eligible reference seasons. The figure does not test a null hypothesis, establish an atmospheric cause of fire, or treat the GWIS counts shown in the table as comparable wildfire occurrence.",
    }
    AUDIT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"table_rows": len(rows), "figure_dpi": dpi, "audit": str(AUDIT.relative_to(ROOT))}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
