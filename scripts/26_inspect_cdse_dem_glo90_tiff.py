"""Reinspecciona con tifffile la muestra DEM ya descargada."""

from __future__ import annotations

import json
from pathlib import Path

import tifffile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "raw" / "copernicus_dem" / "copernicus_dem_glo90_audit.tif"
OUTPUT = ROOT / "results" / "exploratory" / "inspect_cdse_dem_glo90_tiff.json"


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("La reinspección ya existe. No se sobreescriben resultados.")
    image = tifffile.imread(SOURCE)
    audit = {
        "source_file": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
        "format": "TIFF",
        "dtype": str(image.dtype),
        "shape": [int(value) for value in image.shape],
        "minimum_value": float(image.min()),
        "maximum_value": float(image.max()),
        "supersedes": "audit_cdse_dem_glo90.json for pixel decoding only",
        "interpretation_limit": (
            "La muestra confirma decodificación válida de elevación GLO 90 para el rectángulo de control. "
            "No comprueba GLO 30, pendiente, exposición, un recorte de Tolima ni una relación con incendios."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
