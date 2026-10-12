"""Consulta el catálogo oficial CDSE para Sentinel 2 L2A sobre Ortega.

No descarga productos ni bandas. El inventario sirve para auditoría de
disponibilidad y no prueba claridad local o una cicatriz de incendio.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
ENV_FILE = ROOT / ".env"
OUTPUT = ROOT / "results" / "exploratory" / "audit_cdse_sentinel_catalog_ortega_to_2026_09_01.json"
STAC_SEARCH = "https://stac.dataspace.copernicus.eu/v1/search"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def access_token() -> str:
    values = read_env(ENV_FILE)
    request = Request(
        TOKEN_URL,
        data=urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": values["CDSE_CLIENT_ID"],
                "client_secret": values["CDSE_CLIENT_SECRET"],
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        return json.load(response)["access_token"]


def read_ortega_geometry() -> dict:
    with BOUNDARIES.open(encoding="utf-8") as stream:
        for feature in json.load(stream)["features"]:
            if feature.get("properties", {}).get("mun_nombre") == "ORTEGA":
                return feature["geometry"]
    raise ValueError("ORTEGA no está presente en el archivo municipal de Cortolima")


def main() -> None:
    datetime_window = "2026-08-01T00:00:00Z/2026-09-01T23:59:59Z"
    request = Request(
        STAC_SEARCH,
        data=json.dumps(
            {
                "collections": ["sentinel-2-l2a"],
                "intersects": read_ortega_geometry(),
                "datetime": datetime_window,
                "limit": 100,
            }
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/geo+json",
            "Authorization": f"Bearer {access_token()}",
        },
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        response_content_type = response.headers.get_content_type()
        raw_response = response.read().decode("utf-8", errors="replace")
    if response_content_type not in {"application/json", "application/geo+json"}:
        audit = {
            "catalog": STAC_SEARCH,
            "collection": "sentinel-2-l2a",
            "screening_geometry": "Municipio Ortega publicado por Cortolima",
            "datetime_window": datetime_window,
            "catalog_response_content_type": response_content_type,
            "catalog_request_status": "rejected_without_catalogue_payload",
            "interpretation_limit": (
                "La autenticación OAuth no garantiza que este entorno pueda consultar el endpoint STAC. "
                "No se infiere ausencia de productos a partir de esta respuesta."
            ),
        }
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        return
    catalog = json.loads(raw_response)
    scenes = []
    for feature in catalog.get("features", []):
        properties = feature.get("properties", {})
        scenes.append(
            {
                "product_id": feature.get("id"),
                "datetime": properties.get("datetime"),
                "tile_cloud_cover_percent": properties.get("eo:cloud_cover"),
                "mgrs_tile": properties.get("s2:mgrs_tile"),
                "processing_baseline": properties.get("s2:processing_baseline"),
            }
        )
    scenes.sort(key=lambda scene: (scene["datetime"] or "", scene["product_id"] or ""))
    audit = {
        "catalog": STAC_SEARCH,
        "collection": "sentinel-2-l2a",
        "screening_geometry": "Municipio Ortega publicado por Cortolima",
        "datetime_window": datetime_window,
        "scene_count": len(scenes),
        "scenes": scenes,
        "interpretation_limit": (
            "El catálogo oficial confirma metadatos de producto. No prueba claridad local, "
            "cobertura de una cicatriz, calidad de bandas o adecuación para análisis."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
