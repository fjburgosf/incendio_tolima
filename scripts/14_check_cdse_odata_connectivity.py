"""Comprueba la conectividad autorizada con el catálogo OData de CDSE.

No descarga productos, no guarda tokens ni credenciales y no infiere cobertura
espacial o temporal a partir de una respuesta de conectividad.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
ODATA_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products?$top=1"
OUTPUT = ROOT / "results" / "exploratory" / "check_cdse_odata_connectivity.json"


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


def main() -> None:
    request = Request(
        ODATA_URL,
        headers={"Accept": "application/json", "Authorization": f"Bearer {access_token()}"},
    )
    with urlopen(request, timeout=60) as response:
        content_type = response.headers.get_content_type()
        raw = response.read().decode("utf-8", errors="replace")
    audit = {
        "catalog": "https://catalogue.dataspace.copernicus.eu/odata/v1/Products",
        "authorization": "OAuth token checked without persistence",
        "response_content_type": content_type,
    }
    if content_type == "application/json":
        payload = json.loads(raw)
        audit["connectivity"] = "success"
        audit["returned_product_count"] = len(payload.get("value", []))
    else:
        audit["connectivity"] = "response_without_json_payload"
        audit["interpretation_limit"] = "No se infiere ausencia de productos ni fallo de credenciales."
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
