"""Audita la estructura del límite municipal público de Cortolima.

No modifica el archivo crudo. Produce un resumen estructural para la auditoría
de fuentes y no calcula área afectada ni intersecciones con incendios.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
OUTPUT = ROOT / "results" / "exploratory" / "audit_cortolima_municipal_boundaries.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def main() -> None:
    with SOURCE.open(encoding="utf-8") as stream:
        collection = json.load(stream)

    features = collection.get("features", [])
    geometries = Counter(
        feature.get("geometry", {}).get("type", "SIN_GEOMETRIA") for feature in features
    )
    names = sorted(
        feature.get("properties", {}).get("mun_nombre")
        for feature in features
        if feature.get("properties", {}).get("mun_nombre")
    )

    audit = {
        "source_file": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
        "sha256": sha256(SOURCE),
        "collection_type": collection.get("type"),
        "feature_count": len(features),
        "geometry_counts": dict(sorted(geometries.items())),
        "municipality_name_count": len(names),
        "municipality_names": names,
        "interpretation_limit": (
            "Este control solo verifica la estructura de una referencia administrativa. "
            "No delimita incendios ni estima áreas afectadas."
        ),
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
