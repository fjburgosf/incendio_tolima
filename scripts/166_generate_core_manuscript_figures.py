"""Generate the study map and the activity danger decoupling figure at 600 dpi."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parents[1] / ".matplotlib_cache"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from shapely.geometry import shape


ROOT = Path(__file__).resolve().parents[1]
BOUNDARY = ROOT / "data" / "raw" / "boundaries" / "geoBoundaries-COL-ADM1.geojson"
DETECTIONS = ROOT / "data" / "interim" / "regional_fire_detections_labelled_v1.csv"
ACTIVITY = ROOT / "data" / "processed" / "regional_fire_activity_department_season_v1.csv"
DANGER = ROOT / "data" / "processed" / "era5_land_august_season_danger_1991_2026_v1.csv"
OUT = ROOT / "figures" / "revision_v2"
AUDIT = ROOT / "results" / "primary" / "audit_core_manuscript_figures_v1.json"


def draw_geometry(ax, geometry, **kwargs):
    geometries = geometry.geoms if geometry.geom_type == "MultiPolygon" else [geometry]
    for polygon in geometries:
        x, y = polygon.exterior.xy
        ax.fill(x, y, **kwargs)


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("Core manuscript figure audit exists and must not be overwritten")
    OUT.mkdir(parents=True, exist_ok=True)
    boundaries = json.loads(BOUNDARY.read_text(encoding="utf-8"))
    targets = {
        "CO-TOL": "Tolima", "CO-CAL": "Caldas", "CO-CAU": "Cauca", "CO-CUN": "Cundinamarca",
        "CO-HUI": "Huila", "CO-QUI": "Quindio", "CO-RIS": "Risaralda", "CO-VAC": "Valle del Cauca",
    }
    polygons = {}
    for feature in boundaries["features"]:
        name = targets.get(str(feature.get("properties", {}).get("shapeISO", "")))
        if name:
            polygons[name] = shape(feature["geometry"])
    if set(polygons) != set(targets.values()):
        raise RuntimeError("Study region boundary coverage mismatch")
    detections = list(csv.DictReader(DETECTIONS.open(encoding="utf-8")))
    points = [row for row in detections if row["department"] == "Tolima" and row["season"] == "2026"]
    fig, ax = plt.subplots(figsize=(6.2, 6.0), constrained_layout=True)
    for name, geometry in polygons.items():
        draw_geometry(ax, geometry, color="#d9d9d9", edgecolor="white", linewidth=0.65)
        c = geometry.representative_point()
        ax.text(c.x, c.y, name, ha="center", va="center", fontsize=7)
    draw_geometry(ax, polygons["Tolima"], color="#fdd49e", edgecolor="#d95f0e", linewidth=1.0)
    ax.scatter([float(row["longitude"]) for row in points], [float(row["latitude"]) for row in points], s=4, c="#e6550d", alpha=0.55, linewidths=0, label="VIIRS August 2026")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title("Tolima and seven neighboring departments")
    ax.legend(loc="lower left", frameon=False, fontsize=8)
    ax.set_aspect("equal", adjustable="box")
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
    map_path = OUT / "Figure_1_study_area_and_thermal_activity.png"
    fig.savefig(map_path, dpi=600, bbox_inches="tight")
    plt.close(fig)

    activity = pd.read_csv(ACTIVITY)
    danger = pd.read_csv(DANGER)
    tolima = activity.loc[activity["department"] == "Tolima", ["season", "detections"]].merge(
        danger.loc[danger["season"].between(2012, 2026), ["season", "vpd_daily_max_mean_kpa"]], on="season", how="inner"
    )
    if len(tolima) != 15:
        raise RuntimeError("Activity danger merge is incomplete")
    fig, ax = plt.subplots(figsize=(6.6, 4.8), constrained_layout=True)
    reference = tolima.loc[tolima["season"] < 2026]
    ax.scatter(reference["vpd_daily_max_mean_kpa"], reference["detections"], color="#3182bd", s=42, alpha=0.85, label="2012 to 2025")
    target = tolima.loc[tolima["season"] == 2026].iloc[0]
    ax.scatter([target["vpd_daily_max_mean_kpa"]], [target["detections"]], color="#e6550d", s=70, zorder=3, label="2026")
    for _, row in tolima.loc[tolima["season"].isin([2019, 2026])].iterrows():
        ax.annotate(str(int(row["season"])), (row["vpd_daily_max_mean_kpa"], row["detections"]), xytext=(5, 5), textcoords="offset points", fontsize=8)
    ax.set_xlabel("August mean daily maximum VPD in kPa")
    ax.set_ylabel("VIIRS Suomi NPP detections")
    ax.set_title("Tolima August activity and atmospheric demand")
    ax.legend(frameon=False)
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
    scatter_path = OUT / "Figure_2_activity_atmospheric_demand_decoupling.png"
    fig.savefig(scatter_path, dpi=600, bbox_inches="tight")
    plt.close(fig)
    AUDIT.write_text(json.dumps({"script": Path(__file__).name, "figures": [str(map_path.relative_to(ROOT)), str(scatter_path.relative_to(ROOT))], "tolima_rows": len(tolima), "target_detections": int(target["detections"]), "target_vpd": float(target["vpd_daily_max_mean_kpa"])}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(AUDIT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
