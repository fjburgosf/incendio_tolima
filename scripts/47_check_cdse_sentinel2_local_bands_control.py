"""Comprueba disponibilidad y valores de B8 y B12 en un control técnico Sentinel 2."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import tifffile


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
CONTROL = ROOT / "results" / "exploratory" / "define_ortega_firms_technical_control_bbox.json"
RAW_DIR = ROOT / "data" / "raw" / "sentinel2_control"
AUDIT = ROOT / "results" / "exploratory" / "check_cdse_sentinel2_local_bands_control.json"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
DATES = ("2026-07-06", "2026-08-10")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def access_token(values: dict[str, str]) -> str:
    form = urlencode({"grant_type": "client_credentials", "client_id": values["CDSE_CLIENT_ID"], "client_secret": values["CDSE_CLIENT_SECRET"]}).encode("utf-8")
    request = Request(TOKEN_URL, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(request, timeout=60) as response:
        return json.load(response)["access_token"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def body(bbox: list[float], day: str) -> dict:
    next_day = f"{day[:8]}{int(day[8:]) + 1:02d}"
    return {
        "input": {"bounds": {"bbox": bbox, "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"}}, "data": [{"type": "sentinel-2-l2a", "dataFilter": {"timeRange": {"from": f"{day}T00:00:00Z", "to": f"{next_day}T00:00:00Z"}, "mosaickingOrder": "leastCC"}}]},
        "output": {"width": 100, "height": 100, "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}]},
        "evalscript": "//VERSION=3\nfunction setup() { return { input: [\"B08\", \"B12\", \"dataMask\"], output: { bands: 3, sampleType: \"FLOAT32\" } }; }\nfunction evaluatePixel(s) { return [s.B08, s.B12, s.dataMask]; }",
    }


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("El control de bandas Sentinel 2 ya existe.")
    bbox = json.loads(CONTROL.read_text(encoding="utf-8"))["bbox_wgs84"]
    token = access_token(read_env(ENV_FILE))
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    observations = []
    for day in DATES:
        output = RAW_DIR / f"sentinel2_b8_b12_ortega_technical_control_{day}.tif"
        manifest = RAW_DIR / f"sentinel2_b8_b12_ortega_technical_control_{day}_manifest.json"
        if output.exists() or manifest.exists():
            raise FileExistsError(f"Ya existe un insumo para {day}")
        request_body = body(bbox, day)
        request = Request(PROCESS_URL, data=json.dumps(request_body).encode("utf-8"), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=120) as response:
                payload = response.read()
                content_type = response.headers.get_content_type()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"La API Process rechazó la solicitud de {day}: {detail}") from error
        output.write_bytes(payload)
        manifest.write_text(json.dumps({"source": "Copernicus Data Space Process API", "collection": "sentinel-2-l2a", "request": request_body, "file": output.name, "bytes": len(payload), "sha256": sha256(output), "content_type": content_type, "purpose": "Control técnico de bandas. No es una cicatriz."}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        image = tifffile.imread(io.BytesIO(payload))
        mask = image[:, :, 2] > 0
        observations.append({"date": day, "source_file": str(output.relative_to(ROOT)).replace("\\", "/"), "shape": [int(value) for value in image.shape], "valid_pixel_count": int(mask.sum()), "b8_minimum_valid": float(np.min(image[:, :, 0][mask])), "b8_maximum_valid": float(np.max(image[:, :, 0][mask])), "b12_minimum_valid": float(np.min(image[:, :, 1][mask])), "b12_maximum_valid": float(np.max(image[:, :, 1][mask]))})
    result = {"control_source": str(CONTROL.relative_to(ROOT)).replace("\\", "/"), "bbox_wgs84": bbox, "observations": observations, "interpretation_limit": "El control verifica las bandas en un rectángulo técnico. No calcula índice, área quemada, severidad ni una comparación inferencial."}
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
