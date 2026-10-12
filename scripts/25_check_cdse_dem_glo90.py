"""Comprueba acceso a una muestra mínima del DEM Copernicus GLO 90."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import tifffile


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
RAW_DIR = ROOT / "data" / "raw" / "copernicus_dem"
OUTPUT = RAW_DIR / "copernicus_dem_glo90_audit.tif"
MANIFEST = RAW_DIR / "copernicus_dem_glo90_audit_manifest.json"
AUDIT = ROOT / "results" / "exploratory" / "audit_cdse_dem_glo90.json"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
REQUEST_BODY = {
    "input": {
        "bounds": {
            "bbox": [-75.0, 4.3, -74.99, 4.31],
            "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
        },
        "data": [{"type": "dem", "dataFilter": {"demInstance": "COPERNICUS_90"}}],
    },
    "output": {
        "width": 10,
        "height": 10,
        "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
    },
    "evalscript": (
        "//VERSION=3\n"
        "function setup() { return { input: [\"DEM\"], output: { bands: 1, sampleType: \"FLOAT32\" } }; }\n"
        "function evaluatePixel(sample) { return [sample.DEM]; }"
    ),
}


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def access_token(values: dict[str, str]) -> str:
    form = urlencode({
        "grant_type": "client_credentials",
        "client_id": values["CDSE_CLIENT_ID"],
        "client_secret": values["CDSE_CLIENT_SECRET"],
    }).encode("utf-8")
    request = Request(TOKEN_URL, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))["access_token"]


def main() -> None:
    if OUTPUT.exists() or MANIFEST.exists() or AUDIT.exists():
        raise FileExistsError("La comprobación DEM ya existe. No se sobreescriben insumos ni resultados.")
    values = read_env(ENV_FILE)
    token = access_token(values)
    request = Request(
        PROCESS_URL,
        data=json.dumps(REQUEST_BODY).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=120) as response:
        payload = response.read()
        content_type = response.headers.get_content_type()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(payload)
    manifest = {
        "source": "Copernicus Data Space Process API",
        "dem_instance": "COPERNICUS_90",
        "request": REQUEST_BODY,
        "file": OUTPUT.name,
        "bytes": len(payload),
        "sha256": sha256(OUTPUT),
        "content_type": content_type,
        "purpose": "Comprobación de acceso y formato. No es recorte analítico ni capa de pendiente.",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    image = tifffile.imread(io.BytesIO(payload))
    audit = {
        "source_file": str(OUTPUT.relative_to(ROOT)).replace("\\", "/"),
        "format": "TIFF",
        "dtype": str(image.dtype),
        "size": [int(image.shape[1]), int(image.shape[0])],
        "minimum_value": float(image.min()),
        "maximum_value": float(image.max()),
        "interpretation_limit": (
            "La muestra confirma acceso a elevación GLO 90. No comprueba GLO 30, pendiente, exposición, "
            "resolución efectiva sobre Tolima ni una relación con incendios."
        ),
    }
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
