"""Resúmenes suplementarios para el manuscrito, ambos descriptivos.

1. Asociación exploratoria, no prefijada, entre la actividad de la temporada y la
   fracción de detecciones en coberturas naturales, 2012 a 2025, a partir de la
   salida del script 177.
2. Conteo diario de detecciones VIIRS Suomi NPP de la región y de Tolima del 1 al
   8 de agosto y el 17 de agosto de 2026, para cuantificar la ventana de la anomalía
   de la nave declarada por NASA FIRMS, y días de agosto de 2026 sin detecciones.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

from scipy import stats


ROOT = Path(__file__).resolve().parents[1]
COVER = ROOT / "results" / "exploratory" / "tolima_detection_cover_by_season_v1.csv"
ENSO = ROOT / "data" / "interim" / "enso_indices_2012_2026_v1.csv"
DETECTIONS = ROOT / "data" / "interim" / "regional_fire_detections_labelled_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_supplementary_cover_activity_outage_v1.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def main() -> None:
    rows = [r for r in csv.DictReader(COVER.open(encoding="utf-8")) if int(r["season"]) <= 2025]
    oni = {int(r["season"]): float(r["oni_mjj"]) for r in csv.DictReader(ENSO.open(encoding="utf-8"))}
    detections = [int(r["detections"]) for r in rows]
    forest = [float(r["share_point_forest_formation"]) for r in rows]
    natural = [float(r["share_point_forest_formation"]) + float(r["share_point_natural_non_forest"]) for r in rows]
    index = [oni[int(r["season"])] for r in rows]
    association = {}
    for name, x, y in (("forest_share_vs_detections", detections, forest),
                       ("natural_share_vs_detections", detections, natural),
                       ("forest_share_vs_oni_mjj", index, forest),
                       ("natural_share_vs_oni_mjj", index, natural)):
        result = stats.spearmanr(x, y)
        association[name] = {"spearman_rho": round(float(result.statistic), 4), "p_value": float(result.pvalue), "n": len(x)}
    selection = [float(r["selection_ratio_point_forest_formation"]) for r in rows]

    regional, tolima = Counter(), Counter()
    with DETECTIONS.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["season"] != "2026":
                continue
            regional[row["acq_date"]] += 1
            if row["department"] == "Tolima":
                tolima[row["acq_date"]] += 1
    days = [f"2026-08-{d:02d}" for d in range(1, 32)]
    audit = {
        "script": Path(__file__).name,
        "status": "descriptivo. La parte 1 es exploratoria y no estaba en el plan congelado",
        "cover_activity_association_2012_2025": association,
        "forest_selection_ratio_2012_2025": {"min": min(selection), "max": max(selection)},
        "regional_detections_by_day_2026_aug_01_08_and_17": {d: regional.get(d, 0) for d in days[:8] + ["2026-08-17"]},
        "tolima_detections_by_day_2026_aug_01_08_and_17": {d: tolima.get(d, 0) for d in days[:8] + ["2026-08-17"]},
        "regional_days_without_detection_2026": [d for d in days if regional.get(d, 0) == 0],
        "inputs": [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(p)} for p in (COVER, ENSO, DETECTIONS)],
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
