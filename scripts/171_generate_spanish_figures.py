"""Regenerate the five current manuscript figures with Spanish labels.

Reuse the original plotting functions and data. Translate text before export,
redirect generated outputs, and verify that plotted numerical values are intact.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import locale
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "figuras español"
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".matplotlib_cache"))

import matplotlib

matplotlib.use("Agg")
# Decimal point in every figure, independently of the system locale.
locale.setlocale(locale.LC_NUMERIC, "C")
matplotlib.rcParams["axes.formatter.use_locale"] = False
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.figure import Figure
from matplotlib.text import Text
from matplotlib.ticker import FuncFormatter, ScalarFormatter
from PIL import Image


FILENAMES = {
    "Figure_1_study_area_and_thermal_activity.png": "Figura_1_area_de_estudio_y_actividad_termica.jpg",
    "Figure_2_activity_atmospheric_demand_decoupling.png": "Figura_2_actividad_y_demanda_atmosferica.jpg",
    "Figure_3_regional_preseason_validation.png": "Figura_3_validacion_del_modelo_regional.jpg",
    "Figure_4_spatial_association_sensitivities.png": "Figura_4_asociaciones_espaciales_y_sensibilidad.jpg",
    "Figure_5_daily_demand_and_activity.png": "Figura_5_demanda_y_actividad_diarias.jpg",
}
TRANSLATIONS = {
    "VIIRS August 2026": "VIIRS agosto de 2026",
    "Longitude": "Longitud",
    "Latitude": "Latitud",
    "Tolima and seven neighboring departments": "Tolima y siete departamentos vecinos",
    "Quindio": "Quindío",
    "2012 to 2025": "2012 a 2025",
    "August mean daily maximum VPD in kPa": "Promedio de los máximos diarios de DPV en agosto (kPa)",
    "VIIRS Suomi NPP detections": "Detecciones VIIRS Suomi NPP",
    "Tolima August activity and atmospheric demand": "Actividad y demanda atmosférica en agosto en Tolima",
    "Historical baseline": "Referencia histórica",
    "Preseason model": "Modelo antecedente",
    "Observed": "Observado",
    "Tolima, August 2026": "Tolima, agosto de 2026",
    "2026 error": "Error de 2026",
    "Absolute logarithmic error": "Error logarítmico absoluto",
    "Held out historical seasons": "Temporadas históricas de validación",
    "Tolima, validation 2012 to 2025": "Tolima, validación de 2012 a 2025",
    "Odds ratio, logarithmic scale": "Razón de momios, escala logarítmica",
    "Spatial associations within municipalities": "Asociaciones espaciales dentro de los municipios",
    "Daily maximum VPD in kPa": "Máximo diario de DPV (kPa)",
    "Daily VIIRS Suomi NPP detections": "Detecciones diarias VIIRS Suomi NPP",
    "August days in Tolima, 2012 to 2026": "Días de agosto en Tolima, de 2012 a 2026",
    "Percentage of seasonal detections": "Porcentaje de detecciones de la temporada",
    "August season": "Temporada de agosto",
    "Detections on days above the reference threshold": "Detecciones en días que superan\nel umbral de referencia",
}
for source_term, target_term in {"Slope": "Pendiente", "UPRA frontier": "Frontera agrícola UPRA"}.items():
    for source_model, target_model in {
        "Linear elevation": "Elevación lineal",
        "Elevation quartiles": "Elevación por cuartiles",
        "One case per 5 km block": "Un caso por bloque de 5 km",
    }.items():
        TRANSLATIONS[f"{source_term}  {source_model}"] = f"{target_term}\n{target_model}"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plotted_values(figure: Figure) -> str:
    """Fingerprint points, lines, bars and limits independently of wording."""
    h = hashlib.sha256()
    for axis in figure.axes:
        for values in (axis.get_xlim(), axis.get_ylim()):
            h.update(np.asarray(values).tobytes())
        for line in axis.lines:
            # Numeric lines include confidence intervals and threshold markers.
            for values in (line.get_xdata(), line.get_ydata()):
                h.update(np.asarray(values).astype(str).tobytes())
        for collection in axis.collections:
            h.update(np.asarray(collection.get_offsets()).tobytes())
            if hasattr(collection, "get_segments"):
                for segment in collection.get_segments():
                    h.update(np.asarray(segment).tobytes())
        for patch in axis.patches:
            h.update(np.asarray(patch.get_path().vertices).tobytes())
            if hasattr(patch, "get_height"):
                h.update(np.asarray([patch.get_x(), patch.get_y(), patch.get_width(), patch.get_height()]).tobytes())
    return h.hexdigest()


def translated(text: str) -> str:
    if text in TRANSLATIONS:
        return TRANSLATIONS[text]
    match = re.fullmatch(r" Reference 90th percentile, ([0-9.]+) kPa", text)
    if match:
        return "Percentil 90 de referencia\n" + match.group(1) + " kPa"
    return text


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    sources = [
        ROOT / "data/raw/boundaries/geoBoundaries-COL-ADM1.geojson",
        ROOT / "data/interim/regional_fire_detections_labelled_v1.csv",
        ROOT / "data/processed/regional_fire_activity_department_season_v1.csv",
        ROOT / "data/processed/era5_land_august_season_danger_1991_2026_v1.csv",
        ROOT / "results/primary/regional_preseason_prediction_rows_v1.csv",
        ROOT / "results/primary/tolima_spatial_case_control_model_v1.csv",
        ROOT / "data/processed/tolima_daily_vpd_activity_v1.csv",
    ]
    originals = sorted((ROOT / "submission_figures/entramado").glob("*.png"))
    manuscript = ROOT / "manuscript/manuscript_entramado.docx"
    protected = sources + originals + ([manuscript] if manuscript.exists() else [])
    before = {str(p.relative_to(ROOT)): digest(p) for p in protected}
    records = []
    original_savefig = Figure.savefig

    def save_spanish(figure: Figure, fname, *args, **kwargs):
        source_name = Path(fname).name
        output = OUT / FILENAMES[source_name]
        figure.canvas.draw()
        numerical_before = plotted_values(figure)
        for item in figure.findobj(match=Text):
            item.set_text(translated(item.get_text()))
        # Numeric axes keep the native formatter and its decimal point. Only
        # categorical tick labels pass through the translation table.
        for axis in figure.axes:
            for coordinate in (axis.xaxis, axis.yaxis):
                formatter = coordinate.get_major_formatter()
                if not isinstance(formatter, ScalarFormatter):
                    coordinate.set_major_formatter(FuncFormatter(lambda x, pos, original=formatter: translated(original(x, pos))))
        if source_name.startswith("Figure_2"):
            figure.axes[0].xaxis.label.set_fontsize(9)
            figure.axes[0].title.set_fontsize(11)
        if source_name.startswith("Figure_4"):
            figure.set_size_inches(9.8, 5.8)
            figure.axes[0].tick_params(axis="y", labelsize=9)
        if source_name.startswith("Figure_5"):
            figure.set_size_inches(12.4, 4.8)
            figure.tight_layout(pad=1.3, w_pad=3.0)
        figure.canvas.draw()
        texts = [t.get_text() for t in figure.findobj(match=Text) if t.get_text()]
        remaining = [t for t in texts if t in TRANSLATIONS and TRANSLATIONS[t] != t]
        if remaining:
            raise RuntimeError(f"Untranslated labels in {source_name}: {remaining}")
        if numerical_before != plotted_values(figure):
            raise RuntimeError(f"Plotted numerical values changed in {source_name}")
        kwargs["dpi"] = 600
        # JPEG at high quality, without chroma subsampling, so thin lines and text stay sharp.
        kwargs["pil_kwargs"] = {"quality": 95, "subsampling": 0}
        original_savefig(figure, output, *args, **kwargs)
        with Image.open(output) as image:
            dpi = image.info.get("dpi")
            if not dpi or min(dpi) < 599.9:
                raise RuntimeError(f"Insufficient export resolution in {output}")
            records.append({"file": output.name, "original_file": source_name, "pixels": image.size, "dpi": dpi,
                            "sha256": digest(output), "numerical_values_sha256": numerical_before,
                            "numerical_values_unchanged": True, "visible_text": texts})

    Figure.savefig = save_spanish
    try:
        with tempfile.TemporaryDirectory(prefix="tolima_figuras_es_") as scratch:
            for name in ["166_generate_core_manuscript_figures.py", "164_generate_revision_v2_figures.py", "169_generate_daily_vpd_figure.py"]:
                spec = importlib.util.spec_from_file_location("spanish_" + name[:-3], ROOT / "scripts" / name)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.AUDIT = Path(scratch) / (name + ".json")
                if hasattr(module, "OUT"):
                    module.OUT = OUT
                if hasattr(module, "OUTPUT_DIR"):
                    module.OUTPUT_DIR = OUT
                if hasattr(module, "OUTPUT"):
                    module.OUTPUT = OUT / "Figure_5_daily_demand_and_activity.png"
                # Source 164 inspects the paths it passed to savefig afterwards.
                # Supply translated filenames at its image inspection boundary.
                real_open = Image.open
                def inspect_export(fp, *a, **kw):
                    if isinstance(fp, (str, Path)) and Path(fp).name in FILENAMES:
                        fp = OUT / FILENAMES[Path(fp).name]
                    return real_open(fp, *a, **kw)
                Image.open = inspect_export
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        module.main()
                finally:
                    Image.open = real_open
    finally:
        Figure.savefig = original_savefig
        plt.close("all")
    if len(records) != 5:
        raise RuntimeError(f"Expected five figures, obtained {len(records)}")
    after = {str(p.relative_to(ROOT)): digest(p) for p in protected}
    if before != after:
        raise RuntimeError("A protected source or original artifact was changed")
    audit = {"executed": datetime.now().astimezone().isoformat(), "language": "es",
             "script": Path(__file__).name, "protected_files_unchanged": True,
             "source_and_original_sha256": before, "figures": sorted(records, key=lambda r: r["file"])}
    audit_path = ROOT / "results/primary/audit_spanish_figures_v1.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_directory": str(OUT), "figures": [r["file"] for r in audit["figures"]],
                      "protected_files_unchanged": True, "resolution_dpi": 600}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
