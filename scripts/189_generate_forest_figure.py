"""Figura forestal compuesta en español a 600 dpi.

Paneles. (a) Detecciones de agosto de 2026 según la cobertura previa sobre el bosque
de MapBiomas 2024. (b) Razón de selección de la cobertura natural frente a las
detecciones de cada temporada. (c) Razón de tasas de detección del bosque bajo y
sobre 1000 m por temporada. (d) dNBR de Sentinel-2 en la celda corroborada con más
píxeles de bosque útiles, con el bosque y las detecciones. Solo grafica salidas de
los scripts 177, 187, 188 y 190. Punto decimal en todas las etiquetas. Letra Calibri
en tamaño de impresión, con la figura dibujada al ancho de texto de la plantilla.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import locale
import math
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".matplotlib_cache"))
import matplotlib

matplotlib.use("Agg")
locale.setlocale(locale.LC_NUMERIC, "C")
matplotlib.rcParams["axes.formatter.use_locale"] = False
matplotlib.rcParams.update({"font.family": "Calibri", "font.size": 9, "axes.titlesize": 9.5, "axes.labelsize": 9,
                            "xtick.labelsize": 8.5, "ytick.labelsize": 8.5, "legend.fontsize": 8.5})
import matplotlib.pyplot as plt
import numpy as np
import tifffile
from matplotlib.colors import ListedColormap
from shapely.geometry import shape
from shapely.ops import unary_union

COVER_ROWS = ROOT / "data" / "processed" / "tolima_detection_cover_v1.csv"
AUDIT187 = ROOT / "results" / "exploratory" / "audit_review_v3_missing_days_area_rates_blocks_v1.json"
AUDIT190 = ROOT / "results" / "exploratory" / "audit_area_rate_ratio_uncertainty_sensitivity_v1.json"
CELLS188 = ROOT / "results" / "exploratory" / "sentinel2_forest_cell_corroboration_v2.csv"
RAW188 = ROOT / "data" / "raw" / "sentinel2_forest_cells_2026_v2"
DETECTIONS = ROOT / "data" / "interim" / "regional_fire_detections_labelled_v1.csv"
MUNICIPAL = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
MAPBIOMAS_2024 = ROOT / "data" / "raw" / "mapbiomas" / "coverage_collection3" / "colombia_coverage_2024.tif"
OUT = ROOT / "figures" / "Figura_forestal_bosque_y_vegetacion_natural.png"
OUT_JPG = ROOT / "figuras español" / "Figura_2_bosque_y_vegetacion_natural.jpg"
FOREST = {3, 5, 6, 49}


def load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    m177 = load("177_forest_cover_at_detections.py", "c177")
    raster = m177.TiledRaster(MAPBIOMAS_2024)
    municipalities = [shape(f["geometry"]) for f in json.loads(MUNICIPAL.read_text(encoding="utf-8"))["features"]]
    tolima = unary_union(municipalities)
    west, south, east, north = tolima.bounds
    step = 0.006
    lons = np.arange(west, east, step); lats = np.arange(north, south, -step)
    forest_grid = np.zeros((lats.size, lons.size))
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            forest_grid[i, j] = int(raster.window(float(lo), float(la), 0)[0, 0]) in FOREST
    from shapely import contains_xy
    lo_g, la_g = np.meshgrid(lons, lats)
    inside = contains_xy(tolima, lo_g, la_g)
    forest_grid = np.where(inside, forest_grid, np.nan)

    rows = [r for r in csv.DictReader(COVER_ROWS.open(encoding="utf-8")) if r["season"] == "2026"]
    a187 = json.loads(AUDIT187.read_text(encoding="utf-8"))["L_area_denominators"]["per_season"]
    seasons = sorted(int(s) for s in a187)

    fig, axes = plt.subplots(2, 2, figsize=(6.5, 5.5))
    ax = axes[0, 0]
    ax.imshow(forest_grid, extent=[lons[0], lons[-1] + step, lats[-1] - step, lats[0]], cmap=ListedColormap(["#f2f2f2", "#9fd39a"]),
              interpolation="nearest", vmin=0, vmax=1)
    for geom in municipalities:
        for poly in getattr(geom, "geoms", [geom]):
            x, y = poly.exterior.xy
            ax.plot(x, y, color="#9a9a9a", linewidth=0.3)
    styles = {"agricultural_other": ("#8c6d31", "Agropecuario"), "natural_non_forest": ("#e6a700", "No boscosa"),
              "forest_formation": ("#0b5d1e", "Bosque"), "non_vegetated": ("#4f6dc2", "No vegetada")}
    for group, (color, label) in styles.items():
        sub = [r for r in rows if r["cover_group"] == group]
        ax.scatter([float(r["longitude"]) for r in sub], [float(r["latitude"]) for r in sub], s=3, color=color, label=f"{label} ({len(sub)})",
                   linewidths=0)
    ax.set_xlabel("Longitud"); ax.set_ylabel("Latitud")
    ax.set_title("(a) Detecciones de 2026 y bosque de 2024", loc="left")
    ax.legend(loc="upper left", frameon=False, markerscale=2.5, handletextpad=0.1, borderpad=0.2, labelspacing=0.2, borderaxespad=0.2)
    ax.set_aspect("equal")
    ax.set_xlim(west - 0.85, east + 0.05); ax.set_ylim(south - 0.05, north + 0.05)
    ax.set_xticks([-76.5, -75.5, -74.5])

    ax = axes[0, 1]
    det = [a187[str(s)]["detections"] for s in seasons]
    nat = [a187[str(s)]["natural_selection_ratio"] for s in seasons]
    for s, d, n in zip(seasons, det, nat):
        ax.scatter(d, n, s=40 if s == 2026 else 22, color="#b30000" if s == 2026 else "#4d4d4d", zorder=3)
        if s in (2019, 2026, 2021, 2017):
            ax.annotate(str(s), (d, n), textcoords="offset points", xytext=(3, 2), fontsize=8)
    ax.set_xlabel("Detecciones de agosto en Tolima")
    ax.set_ylabel("Razón de selección natural")
    ax.set_title("(b) Cobertura natural y actividad", loc="left")
    ax.grid(alpha=0.3)

    ax = axes[1, 0]
    low = [a187[str(s)]["low_elevation_rate_ratio"] for s in seasons]
    labels = [f"{s % 100:02d}" for s in seasons]
    ax.bar(labels, low, color=["#b30000" if s == 2026 else "#7f7f7f" for s in seasons])
    if AUDIT190.exists():
        lo, hi = json.loads(AUDIT190.read_text(encoding="utf-8"))["Q_uncertainty_2026"]["block_bootstrap"]["low_elevation_rate_ratio"]["interval_95"]
        if np.isfinite(hi):
            ax.errorbar([labels[-1]], [low[-1]], yerr=[[low[-1] - lo], [hi - low[-1]]], color="black", capsize=2, linewidth=0.8)
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(matplotlib.ticker.FixedLocator([2, 5, 10, 20, 50, 100, 200]))
    ax.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, p: f"{v:g}"))
    ax.set_ylim(2, 200)
    ax.set_ylabel("Razón de tasas bajo y sobre 1000 m")
    ax.set_xlabel("Temporada (20xx)")
    ax.set_title("(c) Concentración en bosque bajo", loc="left")
    ax.tick_params(axis="x", labelsize=8)
    ax.grid(axis="y", alpha=0.3)

    cells = [r for r in csv.DictReader(CELLS188.open(encoding="utf-8")) if r["status"] == "corroborated"]
    best = max(cells, key=lambda r: int(r["footprint_forest_usable"]))
    pre = tifffile.imread(RAW188 / f"cell_{best['cell_x']}_{best['cell_y']}_pre_{best['pre_date']}.tif").astype(float)
    post = tifffile.imread(RAW188 / f"cell_{best['cell_x']}_{best['cell_y']}_post_{best['post_date']}.tif").astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        dnbr = (pre[..., 0] - pre[..., 1]) / (pre[..., 0] + pre[..., 1]) - (post[..., 0] - post[..., 1]) / (post[..., 0] + post[..., 1])
    usable = np.isin(pre[..., 2], [4, 5, 6, 7]) & np.isin(post[..., 2], [4, 5, 6, 7])
    dnbr = np.where(usable, dnbr, np.nan)
    m188 = load("188_sentinel2_forest_corroboration_v2.py", "s188")
    cell = [c for c in m188.cells() if c["cell"] == (int(best["cell_x"]), int(best["cell_y"]))][0]
    fp = cell["points"]
    lat0 = float(np.mean([p[0] for p in fp])); lon0 = float(np.mean([p[1] for p in fp]))
    mlon = 111320.0 * math.cos(math.radians(lat0)); mlat = 110574.0
    size = dnbr.shape[0]; offsets = (np.arange(size) + 0.5) * 20.0 - 1000.0
    forest_mask = np.zeros(dnbr.shape)
    for r_ in range(size):
        for c_ in range(size):
            forest_mask[r_, c_] = int(raster.window(lon0 + offsets[c_] / mlon, lat0 - offsets[r_] / mlat, 0)[0, 0]) in FOREST
    ax = axes[1, 1]
    im = ax.imshow(dnbr, extent=[-1000, 1000, -1000, 1000], cmap="RdYlGn_r", vmin=-0.2, vmax=0.6)
    ax.contour(offsets, -offsets, forest_mask, levels=[0.5], colors="#0b5d1e", linewidths=0.8)
    for la, lo in fp:
        ex, ey = (lo - lon0) * mlon, (la - lat0) * mlat
        ax.add_patch(plt.Rectangle((ex - 187.5, ey - 187.5), 375, 375, fill=False, edgecolor="black", linewidth=0.8))
    ax.set_xlabel("Distancia al este (m)"); ax.set_ylabel("Distancia al norte (m)")
    ax.set_xticks([-1000, 0, 1000]); ax.set_yticks([-1000, 0, 1000])
    months = {"07": "julio", "08": "agosto", "09": "septiembre"}
    def nice(d):
        return f"{int(d[8:10])} de {months[d[5:7]]}"
    ax.set_title(f"(d) dNBR, {nice(best['pre_date'])} a {nice(best['post_date'])}", loc="left")
    cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04); cb.set_label("dNBR")
    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True); OUT_JPG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=600)
    fig.savefig(OUT_JPG, dpi=600, pil_kwargs={"quality": 95, "subsampling": 0})
    print(json.dumps({"example_cell": [best["cell_x"], best["cell_y"]], "pre": best["pre_date"], "post": best["post_date"],
                      "footprint_dnbr": best["dnbr_forest_footprint_median"], "reference_dnbr": best["dnbr_forest_reference_median"],
                      "outputs": [str(OUT.relative_to(ROOT)), str(OUT_JPG.relative_to(ROOT))]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
