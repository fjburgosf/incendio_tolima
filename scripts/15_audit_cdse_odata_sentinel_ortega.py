"""Audita metadatos Sentinel 2 L2A de CDSE OData para Ortega.

No descarga productos. El inventario es una prueba de metadatos oficiales y no
demuestra claridad local, una cicatriz de incendio ni selección de imagen.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
ENV_FILE = ROOT / ".env"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
ODATA_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
OUTPUT = ROOT / "results" / "exploratory" / "audit_cdse_odata_sentinel_ortega_to_2026_09_01.json"


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


def point_in_ring(longitude: float, latitude: float, ring: list[list[float]]) -> bool:
    inside = False
    for index, point in enumerate(ring):
        next_point = ring[(index + 1) % len(ring)]
        x1, y1 = point
        x2, y2 = next_point
        if (y1 > latitude) != (y2 > latitude):
            crossing = (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
            if longitude < crossing:
                inside = not inside
    return inside


def ortega_screening_point_wkt() -> str:
    boundary_data = json.loads(BOUNDARIES.read_text(encoding="utf-8"))
    feature = next(
        feature
        for feature in boundary_data["features"]
        if feature.get("properties", {}).get("mun_nombre") == "ORTEGA"
    )
    geometry = feature["geometry"]
    if geometry["type"] != "Polygon":
        raise ValueError("La geometría de Ortega no es un polígono simple")
    ring = geometry["coordinates"][0]
    longitudes = [point[0] for point in ring]
    latitudes = [point[1] for point in ring]
    west, east = min(longitudes), max(longitudes)
    south, north = min(latitudes), max(latitudes)
    for row in range(1, 20):
        latitude = south + (north - south) * row / 20
        for column in range(1, 20):
            longitude = west + (east - west) * column / 20
            if point_in_ring(longitude, latitude, ring):
                return f"POINT({longitude} {latitude})"
    raise ValueError("No se encontró punto interior de cribado para Ortega")


def attribute_value(product: dict, name: str):
    for attribute in product.get("Attributes", []):
        if attribute.get("Name") == name:
            return attribute.get("Value")
    return None


def main() -> None:
    date_start = "2026-08-01T00:00:00.000Z"
    date_end = "2026-09-01T23:59:59.999Z"
    filter_expression = (
        "Collection/Name eq 'SENTINEL-2' and "
        "Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and "
        "att/OData.CSC.StringAttribute/Value eq 'S2MSI2A') and "
        f"OData.CSC.Intersects(area=geography'SRID=4326;{ortega_screening_point_wkt()}') and "
        f"ContentDate/Start ge {date_start} and ContentDate/Start le {date_end}"
    )
    query = urlencode({"$filter": filter_expression, "$expand": "Attributes", "$top": "100"})
    request = Request(
        f"{ODATA_URL}?{query}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {access_token()}"},
    )
    with urlopen(request, timeout=60) as response:
        payload = json.load(response)
    scenes = []
    for product in payload.get("value", []):
        scenes.append(
            {
                "product_id": product.get("Id"),
                "product_name": product.get("Name"),
                "acquisition_start": product.get("ContentDate", {}).get("Start"),
                "cloud_cover_percent": attribute_value(product, "cloudCover"),
                "processing_baseline": attribute_value(product, "processingBaseline"),
                "s3_path": product.get("S3Path"),
            }
        )
    scenes.sort(key=lambda scene: (scene["acquisition_start"] or "", scene["product_name"] or ""))
    audit = {
        "catalog": ODATA_URL,
        "collection": "SENTINEL-2",
        "product_type": "S2MSI2A",
        "screening_geometry": "Punto interior de cribado en el municipio Ortega publicado por Cortolima",
        "datetime_window": f"{date_start}/{date_end}",
        "scene_count": len(scenes),
        "scenes": scenes,
        "interpretation_limit": (
            "Los porcentajes de nube son metadatos de producto. Este inventario no comprueba "
            "claridad local, una cicatriz, calidad de bandas ni pertinencia para análisis."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
