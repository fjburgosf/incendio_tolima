"""Audit public Copernicus Rapid Mapping activations for an August 2026 Tolima fire."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "copernicus_ems" / "public_activations_2026_09_06.json"
OUTPUT = ROOT / "results" / "exploratory" / "audit_copernicus_rapid_mapping_activations_2026_09_06.json"
URL = "https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations-info/"
TOLIMA_BBOX = (-76.2, 2.0, -74.0, 6.2)


def parse_centroid(value: str | None) -> tuple[float, float] | None:
    if not value:
        return None
    match = re.fullmatch(r"POINT \(([-0-9.]+) ([-0-9.]+)\)", value)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("El resultado ya existe y no se sobrescribirá")
    response = requests.get(URL, params={"limit": "1000"}, timeout=90)
    response.raise_for_status()
    payload = response.json()
    if payload.get("next") is not None:
        raise RuntimeError("La respuesta quedó paginada y la auditoría no está completa")
    if RAW.exists():
        if RAW.read_bytes() != response.content:
            raise RuntimeError("El archivo crudo existe con contenido diferente")
    else:
        RAW.parent.mkdir(parents=True, exist_ok=True)
        RAW.write_bytes(response.content)

    activations = payload.get("results", [])
    colombia = [item for item in activations if "Colombia" in item.get("countries", [])]
    august_wildfires = [
        item
        for item in activations
        if str(item.get("eventTime") or "").startswith("2026-08")
        and item.get("category") == "Wildfire"
    ]
    colombia_august_wildfires = [
        item for item in august_wildfires if "Colombia" in item.get("countries", [])
    ]
    centroid_in_tolima = []
    for item in activations:
        centroid = parse_centroid(item.get("centroid"))
        if centroid is None:
            continue
        longitude, latitude = centroid
        if TOLIMA_BBOX[0] <= longitude <= TOLIMA_BBOX[2] and TOLIMA_BBOX[1] <= latitude <= TOLIMA_BBOX[3]:
            centroid_in_tolima.append(item)

    result = {
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "official_endpoint": URL,
        "raw_path": str(RAW.relative_to(ROOT)).replace("\\", "/"),
        "raw_sha256": hashlib.sha256(response.content).hexdigest().upper(),
        "public_activation_count": len(activations),
        "pagination_complete": payload.get("next") is None,
        "colombia_activation_count": len(colombia),
        "colombia_activations": [
            {key: item.get(key) for key in ["code", "name", "eventTime", "category", "centroid"]}
            for item in colombia
        ],
        "august_2026_wildfire_activation_count_worldwide": len(august_wildfires),
        "august_2026_wildfire_activation_count_colombia": len(colombia_august_wildfires),
        "activation_centroid_count_in_tolima_screening_bbox": len(centroid_in_tolima),
        "activations_with_centroid_in_tolima_screening_bbox": [
            {key: item.get(key) for key in ["code", "name", "eventTime", "category", "centroid"]}
            for item in centroid_in_tolima
        ],
        "interpretation_limit": (
            "Absence from the public activation list means no matching public Rapid Mapping "
            "activation was found at the access time. It does not prove that no other institution "
            "mapped the fires or that no non-public emergency product exists."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
