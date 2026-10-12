"""Audita metadatos Sentinel 2 L2A de julio para un punto de cribado en Ortega."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
HELPER_SCRIPT = ROOT / "scripts" / "15_audit_cdse_odata_sentinel_ortega.py"
ODATA_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
OUTPUT = ROOT / "results" / "exploratory" / "audit_cdse_odata_sentinel_ortega_july_2026.json"


def load_helper():
    specification = importlib.util.spec_from_file_location("odata_ortega", HELPER_SCRIPT)
    if specification is None or specification.loader is None:
        raise RuntimeError("No se pudo cargar el helper OData")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("La auditoría Sentinel de julio ya existe.")
    helper = load_helper()
    start = "2026-07-01T00:00:00.000Z"
    end = "2026-07-31T23:59:59.999Z"
    expression = (
        "Collection/Name eq 'SENTINEL-2' and "
        "Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and "
        "att/OData.CSC.StringAttribute/Value eq 'S2MSI2A') and "
        f"OData.CSC.Intersects(area=geography'SRID=4326;{helper.ortega_screening_point_wkt()}') and "
        f"ContentDate/Start ge {start} and ContentDate/Start le {end}"
    )
    query = urlencode({"$filter": expression, "$expand": "Attributes", "$top": "100"})
    request = Request(f"{ODATA_URL}?{query}", headers={"Accept": "application/json", "Authorization": f"Bearer {helper.access_token()}"})
    with urlopen(request, timeout=60) as response:
        products = json.load(response).get("value", [])
    scenes = []
    for product in products:
        scenes.append({"product_id": product.get("Id"), "product_name": product.get("Name"), "acquisition_start": product.get("ContentDate", {}).get("Start"), "cloud_cover_percent": helper.attribute_value(product, "cloudCover"), "s3_path": product.get("S3Path")})
    scenes.sort(key=lambda item: (item["acquisition_start"] or "", item["product_name"] or ""))
    result = {"catalog": ODATA_URL, "screening_geometry": "Punto interior de cribado en Ortega", "datetime_window": f"{start}/{end}", "scene_count": len(scenes), "scenes": scenes, "interpretation_limit": "Son metadatos de tesela. No prueban claridad local, condiciones previas al incendio ni una cicatriz."}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
