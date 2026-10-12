"""Verifica dos registros bibliográficos y calcula los valores ausentes de tablas.

Primero consulta Crossref para comprobar el año de Zhang y los datos editoriales de
Armenteras, que la revisión previa al envío señaló como incorrectos o incompletos.
No se acepta ninguna corrección sin respuesta del servicio.

Después calcula, con la misma ecuación del manuscrito, la puntuación robusta de las
celdas activas y de la precipitación antecedente, y la mediana histórica de celdas
activas por departamento, que la Tabla 4 necesita para ser auditable.
"""

from __future__ import annotations

import csv
import json
import urllib.request
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
ACTIVITY = ROOT / "data" / "processed" / "regional_fire_activity_department_season_v1.csv"
PANEL = ROOT / "data" / "processed" / "regional_preseason_prediction_panel_v1.csv"
AUDIT = ROOT / "results" / "primary" / "audit_table_completion_and_bibliography_v1.json"
TARGET = 2026
DOIS = {"Zhang2024A": "10.1088/1748-9326/ad928f", "Armenteras2022Cambios": "10.18257/raccefyn.1514"}


def crossref(doi: str) -> dict:
    url = f"https://api.crossref.org/works/{doi}"
    request = urllib.request.Request(url, headers={"User-Agent": "reproducible-audit/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.loads(response.read().decode("utf-8"))["message"]
    issued = payload.get("issued", {}).get("date-parts", [[None]])[0]
    published = payload.get("published", {}).get("date-parts", [[None]])[0]
    return {
        "doi": payload.get("DOI"),
        "title": (payload.get("title") or [""])[0],
        "container": (payload.get("container-title") or [""])[0],
        "volume": payload.get("volume"),
        "issue": payload.get("issue"),
        "page": payload.get("page"),
        "article_number": payload.get("article-number"),
        "issued_year": issued[0] if issued else None,
        "published_year": published[0] if published else None,
    }


def robust_z(observed: float, reference: list) -> dict:
    values = np.asarray(reference, dtype=float)
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    return {
        "observed": observed,
        "reference_median": round(median, 4),
        "reference_mad": round(mad, 4),
        "robust_z": round((observed - median) / (1.4826 * mad), 4) if mad > 0 else None,
    }


def read_csv(path: Path) -> list:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    bibliography = {key: crossref(doi) for key, doi in DOIS.items()}

    rows = read_csv(ACTIVITY)
    tolima = {int(row["season"]): row for row in rows if row["department"] == "Tolima"}
    reference_seasons = [s for s in sorted(tolima) if s != TARGET]

    cells = robust_z(
        float(tolima[TARGET]["cells_1km_with_detection"]),
        [float(tolima[s]["cells_1km_with_detection"]) for s in reference_seasons],
    )
    detections = robust_z(
        float(tolima[TARGET]["detections"]),
        [float(tolima[s]["detections"]) for s in reference_seasons],
    )

    precipitation = None
    panel_rows = read_csv(PANEL)
    field = next((name for name in panel_rows[0] if "may" in name.lower() and "jul" in name.lower()), None)
    if field:
        series = {int(row["season"]): float(row[field]) for row in panel_rows if row["department"] == "Tolima"}
        precipitation = robust_z(series[TARGET], [series[s] for s in sorted(series) if s != TARGET])
        precipitation["field"] = field

    medians = {}
    for department in sorted({row["department"] for row in rows}):
        history = [
            float(row["cells_1km_with_detection"])
            for row in rows
            if row["department"] == department and int(row["season"]) != TARGET
        ]
        observed = next(
            float(row["cells_1km_with_detection"])
            for row in rows
            if row["department"] == department and int(row["season"]) == TARGET
        )
        median = float(np.median(history))
        medians[department] = {
            "historical_median_active_cells": round(median, 1),
            "observed_2026": observed,
            "ratio": round(observed / median, 2) if median else None,
        }

    audit = {
        "script": Path(__file__).name,
        "executed": "2026-09-21",
        "crossref_records": bibliography,
        "robust_z_detections": detections,
        "robust_z_active_cells": cells,
        "robust_z_may_to_july_precipitation": precipitation,
        "historical_median_active_cells_by_department": medians,
    }
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
