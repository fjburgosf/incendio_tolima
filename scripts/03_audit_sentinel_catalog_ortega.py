"""Consulta metadatos Sentinel 2 L2A para el municipio de cribado Ortega.

La consulta verifica disponibilidad de productos, no descarga bandas ni estima
área quemada, severidad, nubosidad local o una cicatriz de incendio.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
OUTPUT = ROOT / "results" / "exploratory" / "audit_sentinel_catalog_ortega_august_2026.json"
STAC_SEARCH = "https://earth-search.aws.element84.com/v1/search"


def read_ortega_geometry() -> dict:
    with BOUNDARIES.open(encoding="utf-8") as stream:
        features = json.load(stream)["features"]
    for feature in features:
        if feature.get("properties", {}).get("mun_nombre") == "ORTEGA":
            return feature["geometry"]
    raise ValueError("ORTEGA no está presente en el archivo municipal de Cortolima")


def request_catalog(payload: dict) -> dict:
    request = Request(
        STAC_SEARCH,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def main() -> None:
    payload = {
        "collections": ["sentinel-2-l2a"],
        "intersects": read_ortega_geometry(),
        "datetime": "2026-08-01T00:00:00Z/2026-08-31T23:59:59Z",
        "limit": 100,
        "query": {"eo:cloud_cover": {"lte": 25}},
    }
    catalog = request_catalog(payload)
    scenes = []
    for feature in catalog.get("features", []):
        properties = feature.get("properties", {})
        assets = feature.get("assets", {})
        scenes.append(
            {
                "scene_id": feature.get("id"),
                "datetime": properties.get("datetime"),
                "tile_cloud_cover_percent": properties.get("eo:cloud_cover"),
                "mgrs_tile": properties.get("s2:mgrs_tile"),
                "has_nir": "nir" in assets,
                "has_swir22": "swir22" in assets,
                "has_scl": "scl" in assets,
            }
        )
    scenes.sort(key=lambda scene: (scene["datetime"] or "", scene["scene_id"] or ""))
    audit = {
        "catalog": STAC_SEARCH,
        "collection": "sentinel-2-l2a",
        "screening_geometry": "Municipio Ortega publicado por Cortolima",
        "datetime_window": "2026-08-01T00:00:00Z/2026-08-31T23:59:59Z",
        "tile_cloud_cover_filter_percent": 25,
        "scene_count": len(scenes),
        "scenes": scenes,
        "interpretation_limit": (
            "La nubosidad corresponde a tesela. Este inventario no comprueba claridad local, "
            "cobertura de una cicatriz, ni viabilidad de una comparación antes y después."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
