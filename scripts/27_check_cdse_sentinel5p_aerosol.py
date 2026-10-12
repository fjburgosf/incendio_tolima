"""Comprueba una muestra Sentinel 5P de índice de aerosoles con máscara de datos."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import tifffile


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
RAW_DIR = ROOT / "data" / "raw" / "sentinel5p"
OUTPUT = RAW_DIR / "sentinel5p_aerosol_index_2026-08-20_audit.tif"
MANIFEST = RAW_DIR / "sentinel5p_aerosol_index_2026-08-20_audit_manifest.json"
AUDIT = ROOT / "results" / "exploratory" / "audit_cdse_sentinel5p_aerosol_2026-08-20.json"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
REQUEST_BODY = {
    "input": {
        "bounds": {
            "bbox": [-75.0, 4.3, -74.99, 4.31],
            "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
        },
        "data": [{
            "type": "sentinel-5p-l2",
            "dataFilter": {
                "timeRange": {"from": "2026-08-20T00:00:00Z", "to": "2026-08-21T00:00:00Z"},
            },
            "processing": {"minQa": 50},
        }],
    },
    "output": {
        "width": 10,
        "height": 10,
        "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
    },
    "evalscript": (
        "//VERSION=3\n"
        "function setup() { return { input: [\"AER_AI_340_380\", \"dataMask\"], "
        "output: { bands: 2, sampleType: \"FLOAT32\" } }; }\n"
        "function evaluatePixel(sample) { return [sample.AER_AI_340_380, sample.dataMask]; }"
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
        raise FileExistsError("La comprobación Sentinel 5P ya existe. No se sobreescriben insumos ni resultados.")
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
        "collection": "sentinel-5p-l2",
        "request": REQUEST_BODY,
        "file": OUTPUT.name,
        "bytes": len(payload),
        "sha256": sha256(OUTPUT),
        "content_type": content_type,
        "purpose": "Comprobación de acceso y píxeles válidos. No es una estimación de humo o exposición.",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    image = tifffile.imread(io.BytesIO(payload))
    aerosol = image[:, :, 0]
    mask = image[:, :, 1] > 0
    valid = aerosol[mask]
    audit = {
        "source_file": str(OUTPUT.relative_to(ROOT)).replace("\\", "/"),
        "format": "TIFF",
        "dtype": str(image.dtype),
        "shape": [int(value) for value in image.shape],
        "valid_pixel_count": int(mask.sum()),
        "total_pixel_count": int(mask.size),
        "minimum_valid_aerosol_index": float(np.min(valid)) if valid.size else None,
        "maximum_valid_aerosol_index": float(np.max(valid)) if valid.size else None,
        "interpretation_limit": (
            "La muestra comprueba disponibilidad de píxeles válidos en la fecha de control. No atribuye aerosoles "
            "a incendios, no estima exposición humana y no describe Tolima completo."
        ),
    }
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
