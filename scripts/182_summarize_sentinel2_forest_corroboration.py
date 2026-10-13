"""Resume la distribución de dNBR de la salida del script 181 para el manuscrito.

Descriptivo. No cambia la regla de corroboración ni la clasificación de las celdas.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CELLS = ROOT / "results" / "exploratory" / "sentinel2_forest_cell_corroboration_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_sentinel2_forest_corroboration_summary_v1.json"


def main() -> None:
    cells = pd.read_csv(CELLS)
    evaluable = cells[cells["evaluable"]]
    summary = {
        "script": Path(__file__).name,
        "input": {"path": str(CELLS.relative_to(ROOT)).replace("\\", "/"), "sha256": hashlib.sha256(CELLS.read_bytes()).hexdigest().upper()},
        "cells": int(len(cells)),
        "evaluable_cells": int(len(evaluable)),
        "corroborated_cells": int(cells["corroborated"].sum()),
        "forest_detections_in_corroborated_cells": int(cells.loc[cells["corroborated"], "forest_detections"].sum()),
        "forest_detections_total": int(cells["forest_detections"].sum()),
        "evaluable_fraction_footprint_dnbr_at_least_0_10": round(float((evaluable["dnbr_forest_footprint_median"] >= 0.10).mean()), 4),
        "evaluable_median_footprint_dnbr": round(float(evaluable["dnbr_forest_footprint_median"].median()), 4),
        "evaluable_median_reference_dnbr": round(float(evaluable["dnbr_reference_median"].median()), 4),
        "evaluable_median_footprint_minus_reference": round(float((evaluable["dnbr_forest_footprint_median"] - evaluable["dnbr_reference_median"]).median()), 4),
    }
    AUDIT.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
