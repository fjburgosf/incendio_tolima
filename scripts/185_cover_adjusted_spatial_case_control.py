"""Modelo espacial condicional con la fracción de cobertura natural de cada celda.

Sigue la parte J de `configs/review_robustness_plan_v2.json`, congelada antes de
ejecutar. Muestrea MapBiomas 2024 en una malla de 11 por 11 puntos dentro de cada
celda de 1 km del panel de casos y controles del script 162 y repite la regresión
logística condicional del script 163 con la fracción natural como cuarto término.
La accesibilidad no se incluye porque la capa vial no pudo descargarse íntegra.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer
from statsmodels.discrete.conditional_models import ConditionalLogit
from statsmodels.tools.sm_exceptions import ConvergenceWarning


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "review_robustness_plan_v2.json"
PANEL = ROOT / "data" / "processed" / "tolima_spatial_case_control_panel_v2.csv"
MAPBIOMAS_2024 = ROOT / "data" / "raw" / "mapbiomas" / "coverage_collection3" / "colombia_coverage_2024.tif"
PANEL_OUT = ROOT / "data" / "processed" / "tolima_spatial_case_control_panel_cover_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_cover_adjusted_spatial_case_control_v1.json"
NATURAL = {3, 5, 6, 49, 11, 12, 13, 29, 32, 50, 81, 82}
FOREST = {3, 5, 6, 49}
CRS_PROJECTED = "EPSG:3116"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def scaled(values: pd.Series) -> np.ndarray:
    med = float(values.median()); q1, q3 = values.quantile([0.25, 0.75]); iqr = float(q3 - q1)
    if not math.isfinite(iqr) or iqr <= 0:
        raise RuntimeError(f"Rango intercuartílico no positivo para {values.name}")
    return (values.to_numpy(float) - med) / iqr


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribe")
    spec = importlib.util.spec_from_file_location("cover177", ROOT / "scripts" / "177_forest_cover_at_detections.py")
    m177 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m177)
    raster = m177.TiledRaster(MAPBIOMAS_2024)
    panel = pd.read_csv(PANEL)
    to_wgs = Transformer.from_crs(CRS_PROJECTED, "EPSG:4326", always_xy=True)
    offsets = (np.arange(11) - 5) * (1000.0 / 11)
    natural, forest = [], []
    for row in panel.itertuples():
        xs, ys = np.meshgrid(row.centre_easting_m + offsets, row.centre_northing_m + offsets)
        lon, lat = to_wgs.transform(xs.ravel(), ys.ravel())
        codes = [int(raster.window(float(a), float(b), 0)[0, 0]) for a, b in zip(lon, lat)]
        natural.append(float(np.mean([c in NATURAL for c in codes])))
        forest.append(float(np.mean([c in FOREST for c in codes])))
    panel["natural_cover_fraction"] = natural
    panel["forest_cover_fraction"] = forest
    panel.to_csv(PANEL_OUT, index=False)

    exog = np.column_stack([scaled(panel["elevation_m"]), scaled(panel["slope_degrees"]),
                            scaled(panel["upra_frontier_fraction"]), scaled(panel["natural_cover_fraction"])])
    names = ["elevation_m_per_iqr", "slope_degrees_per_iqr", "upra_frontier_fraction_per_iqr", "natural_cover_fraction_per_iqr"]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = ConditionalLogit(panel["case"].to_numpy(int), exog, groups=panel["stratum"].to_numpy(int)).fit(disp=False, maxiter=500)
    failures = [str(w.message) for w in caught if issubclass(w.category, (ConvergenceWarning, RuntimeWarning))]
    if failures:
        raise RuntimeError(f"Fallo de convergencia, {failures}")
    params, ci, pv = np.asarray(result.params), np.asarray(result.conf_int()), np.asarray(result.pvalues)
    terms = [{"term": n, "odds_ratio": round(math.exp(params[i]), 3), "ci95": [round(math.exp(ci[i, 0]), 3), round(math.exp(ci[i, 1]), 3)],
              "p_value": float(pv[i])} for i, n in enumerate(names)]
    cases = panel[panel["case"] == 1]
    controls = panel[panel["case"] == 0]
    audit = {
        "script": Path(__file__).name, "plan_sha256": sha256(PLAN), "status": "descriptivo y posterior",
        "cases": int(len(cases)), "controls": int(len(controls)),
        "terms": terms,
        "mean_natural_cover_cases_controls": [round(float(cases["natural_cover_fraction"].mean()), 4), round(float(controls["natural_cover_fraction"].mean()), 4)],
        "mean_forest_cover_cases_controls": [round(float(cases["forest_cover_fraction"].mean()), 4), round(float(controls["forest_cover_fraction"].mean()), 4)],
        "inputs": [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(p)} for p in (PANEL, MAPBIOMAS_2024)],
        "output": str(PANEL_OUT.relative_to(ROOT)).replace("\\", "/"),
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: audit[k] for k in ("cases", "controls", "terms", "mean_natural_cover_cases_controls", "mean_forest_cover_cases_controls")}, indent=1))


if __name__ == "__main__":
    main()
