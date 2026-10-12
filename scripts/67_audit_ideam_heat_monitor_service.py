"""Audit the public IDEAM heat-point monitoring FeatureServer for Tolima."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "ideam_heat_monitor"
OUTPUT = ROOT / "results" / "exploratory" / "audit_ideam_heat_monitor_service_2026_09_06.json"
BASE = (
    "https://visualizador.ideam.gov.co/gisserver/rest/services/"
    "Tematica/Monitoreo_Inc_Cobertura_AP/FeatureServer"
)
ACCESS_DATE = "2026-09-06"
LAYERS = [2, 3, 6, 7]


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest().upper()


def preserve_raw(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError(f"Raw file changed and will not be overwritten: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def request_json(url: str, params: dict[str, str]) -> tuple[bytes, dict]:
    response = requests.get(url, params=params, timeout=90)
    response.raise_for_status()
    payload = response.json()
    return response.content, payload


def field_map(definition: dict) -> dict[str, str]:
    return {field["name"]: field["type"] for field in definition.get("fields", [])}


def department_where(fields: dict[str, str]) -> str:
    if "cod_dane_dpto" in fields:
        value = "'73'" if fields["cod_dane_dpto"] == "esriFieldTypeString" else "73"
        return f"cod_dane_dpto={value}"
    value = "'73'" if fields.get("cod_dane") == "esriFieldTypeString" else "73"
    return f"cod_dane={value}"


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("El resultado ya existe y no se sobrescribirá")

    records = []
    for layer_id in LAYERS:
        definition_bytes, definition = request_json(f"{BASE}/{layer_id}", {"f": "json"})
        definition_path = RAW_DIR / f"layer_{layer_id}_definition_{ACCESS_DATE}.json"
        preserve_raw(definition_path, definition_bytes)
        fields = field_map(definition)
        dept_where = department_where(fields)
        date_where = (
            f"{dept_where} AND acq_date >= DATE '2026-08-01' "
            "AND acq_date < DATE '2026-09-01'"
        )

        count_bytes, count_payload = request_json(
            f"{BASE}/{layer_id}/query",
            {"where": dept_where, "returnCountOnly": "true", "f": "json"},
        )
        count_path = RAW_DIR / f"layer_{layer_id}_tolima_count_{ACCESS_DATE}.json"
        preserve_raw(count_path, count_bytes)

        august_bytes, august_payload = request_json(
            f"{BASE}/{layer_id}/query",
            {"where": date_where, "returnCountOnly": "true", "f": "json"},
        )
        august_path = RAW_DIR / f"layer_{layer_id}_tolima_august_count_{ACCESS_DATE}.json"
        preserve_raw(august_path, august_bytes)

        sample_fields = [
            name
            for name in [
                "objectid",
                "idptocalor",
                "acq_date",
                "fechaadq",
                "fechadqloc",
                "satellite",
                "fuente",
                "confidence",
                "frp",
                "nom_dpto",
                "nom_mpio",
                "nom_dpto_1",
                "nom_mpio_1",
                "codigo",
                "leyenda",
                "tipocombst",
                "area_ha",
            ]
            if name in fields
        ]
        sample_bytes, sample_payload = request_json(
            f"{BASE}/{layer_id}/query",
            {
                "where": dept_where,
                "outFields": ",".join(sample_fields),
                "returnGeometry": "false",
                "orderByFields": "objectid DESC",
                "resultRecordCount": "5",
                "f": "json",
            },
        )
        sample_path = RAW_DIR / f"layer_{layer_id}_tolima_latest_sample_{ACCESS_DATE}.json"
        preserve_raw(sample_path, sample_bytes)

        records.append(
            {
                "layer_id": layer_id,
                "name": definition.get("name"),
                "geometry_type": definition.get("geometryType"),
                "max_record_count": definition.get("maxRecordCount"),
                "department_filter": dept_where,
                "august_filter": date_where,
                "total_tolima_response": count_payload,
                "august_2026_tolima_response": august_payload,
                "latest_sample_response": sample_payload,
                "field_types": fields,
                "raw_definition": {
                    "path": str(definition_path.relative_to(ROOT)).replace("\\", "/"),
                    "sha256": sha256_bytes(definition_bytes),
                },
                "raw_total_count": {
                    "path": str(count_path.relative_to(ROOT)).replace("\\", "/"),
                    "sha256": sha256_bytes(count_bytes),
                },
                "raw_august_count": {
                    "path": str(august_path.relative_to(ROOT)).replace("\\", "/"),
                    "sha256": sha256_bytes(august_bytes),
                },
                "raw_latest_sample": {
                    "path": str(sample_path.relative_to(ROOT)).replace("\\", "/"),
                    "sha256": sha256_bytes(sample_bytes),
                },
            }
        )

    result = {
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "official_service": BASE,
        "layers": records,
        "interpretation_limit": (
            "The polygon layers are enriched footprints associated with heat detections. "
            "Their presence does not make them burned-area perimeters, independent fire events, "
            "or validated fire severity. This audit only checks service structure and availability."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
