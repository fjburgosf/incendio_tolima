"""Figura 1 en español con letra Calibri de 10 puntos a tamaño de impresión.

Redibuja el mapa del script 166 (Tolima, sus siete vecinos y las detecciones VIIRS
de agosto de 2026 asignadas a Tolima) con etiquetas en español, dibujado al ancho
con que se inserta en el manuscrito, 12.78 cm, para que la letra se imprima a 10
puntos. Usa los mismos datos y comprueba que el número de puntos coincide con el
de la serie regional. Punto decimal en todas las etiquetas.
"""

from __future__ import annotations

import csv
import hashlib
import json
import locale
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".matplotlib_cache"))
import matplotlib

matplotlib.use("Agg")
locale.setlocale(locale.LC_NUMERIC, "C")
matplotlib.rcParams["axes.formatter.use_locale"] = False
matplotlib.rcParams.update({"font.family": "Calibri", "font.size": 10, "axes.titlesize": 10, "axes.labelsize": 10,
                            "xtick.labelsize": 10, "ytick.labelsize": 10, "legend.fontsize": 10})
import matplotlib.pyplot as plt
from matplotlib import font_manager
from shapely.geometry import shape

BOUNDARY = ROOT / "data" / "raw" / "boundaries" / "geoBoundaries-COL-ADM1.geojson"
DETECTIONS = ROOT / "data" / "interim" / "regional_fire_detections_labelled_v1.csv"
OUT_PNG = ROOT / "figures" / "Figura_1_area_de_estudio_y_actividad_termica_calibri.png"
OUT_JPG = ROOT / "figuras español" / "Figura_1_area_de_estudio_y_actividad_termica.jpg"
AUDIT = ROOT / "results" / "primary" / "audit_figure1_calibri_v1.json"
WIDTH_IN = 4600000 / 914400
NAMES = {"CO-TOL": "Tolima", "CO-CAL": "Caldas", "CO-CAU": "Cauca", "CO-CUN": "Cundinamarca", "CO-HUI": "Huila",
         "CO-QUI": "Quindío", "CO-RIS": "Risaralda", "CO-VAC": "Valle del Cauca"}
LABEL_AT = {"Tolima": (-75.05, 3.42), "Caldas": (-75.35, 5.42)}
ARROW = {"Quindío": (-76.45, 4.6)}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def draw(ax, geometry, **kwargs):
    for polygon in (geometry.geoms if geometry.geom_type == "MultiPolygon" else [geometry]):
        x, y = polygon.exterior.xy
        ax.fill(x, y, **kwargs)


def main() -> None:
    font = font_manager.findfont(font_manager.FontProperties(family="Calibri"), fallback_to_default=False)
    polygons = {}
    for feature in json.loads(BOUNDARY.read_text(encoding="utf-8"))["features"]:
        name = NAMES.get(str(feature.get("properties", {}).get("shapeISO", "")))
        if name:
            polygons[name] = shape(feature["geometry"])
    if set(polygons) != set(NAMES.values()):
        raise RuntimeError("Faltan departamentos en los límites")
    points = [r for r in csv.DictReader(DETECTIONS.open(encoding="utf-8")) if r["department"] == "Tolima" and r["season"] == "2026"]
    if len(points) != 685:
        raise RuntimeError(f"Se esperaban 685 detecciones de Tolima en 2026 y hay {len(points)}")

    fig, ax = plt.subplots(figsize=(WIDTH_IN, 5.25), constrained_layout=True)
    for name, geometry in polygons.items():
        draw(ax, geometry, color="#d9d9d9", edgecolor="white", linewidth=0.65)
    draw(ax, polygons["Tolima"], color="#fdd49e", edgecolor="#d95f0e", linewidth=1.0)
    ax.scatter([float(r["longitude"]) for r in points], [float(r["latitude"]) for r in points], s=3, c="#e6550d",
               alpha=0.6, linewidths=0, label="Detecciones VIIRS de agosto de 2026")
    for name, geometry in polygons.items():
        c = geometry.representative_point()
        if name in ARROW:
            ax.annotate(name, (c.x, c.y), xytext=ARROW[name], ha="center", va="center",
                        arrowprops={"arrowstyle": "-", "color": "#555555", "linewidth": 0.6})
        else:
            x, y = LABEL_AT.get(name, (c.x, c.y))
            ax.text(x, y, name, ha="center", va="center")
    ax.set_xlabel("Longitud"); ax.set_ylabel("Latitud")
    ax.set_title("Tolima y siete departamentos vecinos")
    ax.legend(loc="lower left", frameon=False, markerscale=2.5, handletextpad=0.2)
    ax.set_aspect("equal", adjustable="box")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PNG, dpi=600)
    fig.savefig(OUT_JPG, dpi=600, pil_kwargs={"quality": 95, "subsampling": 0})
    plt.close(fig)
    audit = {"script": Path(__file__).name, "font_file": font, "font_size_points": 10, "width_cm": round(WIDTH_IN * 2.54, 2),
             "points_plotted": len(points), "outputs": [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(p)} for p in (OUT_PNG, OUT_JPG)],
             "inputs": [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(p)} for p in (BOUNDARY, DETECTIONS)]}
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False))


if __name__ == "__main__":
    main()
