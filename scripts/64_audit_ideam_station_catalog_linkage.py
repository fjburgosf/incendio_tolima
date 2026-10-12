"""Download and audit official IDEAM metadata for precipitation station codes.

The script preserves the ArcGIS layer definition and query response as raw JSON.
It then resolves coded domains and compares the official municipality with the
municipality implied only by the name of each manually downloaded CSV.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "ideam_station_catalog"
INTERIM_DIR = ROOT / "data" / "interim"
RESULTS_DIR = ROOT / "results" / "exploratory"

SERVICE = (
    "https://visualizador.ideam.gov.co/gisserver/rest/services/"
    "CNE/CatalogoNacionalEstaciones/FeatureServer/0"
)
ACCESS_DATE = "2026-09-06"

EXPECTED_FILES = {
    "Precipitacionchaparral.csv": ("Chaparral", ["22045010", "22045020", "22060080", "22060110"]),
    "PrecipitacionCoyaima.csv": ("Coyaima", ["22050030", "22050100", "22060100"]),
    "PrecipitacionGuamo.csv": ("Guamo", ["21180160", "21185030"]),
    "PrecipitacionOrtega.csv": ("Ortega", ["22060070", "22060090"]),
    "PrecipitacionSanLuis.csv": ("San Luis", ["21180120", "21210190"]),
}


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest().upper()


def preserve_raw(path: Path, content: bytes) -> None:
    """Create a raw file once and refuse to overwrite different content."""
    if path.exists():
        previous = path.read_bytes()
        if previous != content:
            raise RuntimeError(
                f"Raw file already exists with different content: {path}. "
                "Use a new dated filename instead of overwriting it."
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def get_json(url: str, params: dict[str, str]) -> tuple[bytes, dict]:
    response = requests.get(url, params=params, timeout=90)
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(json.dumps(payload["error"], ensure_ascii=False))
    return response.content, payload


def domain_maps(layer_definition: dict) -> dict[str, dict[str, str]]:
    maps: dict[str, dict[str, str]] = {}
    for field in layer_definition.get("fields", []):
        coded_values = (field.get("domain") or {}).get("codedValues") or []
        maps[field["name"]] = {str(item["code"]): item["name"] for item in coded_values}
    return maps


def domain_value(maps: dict[str, dict[str, str]], field: str, value: object) -> str | None:
    if value is None:
        return None
    candidates = [str(value)]
    if isinstance(value, float) and value.is_integer():
        candidates.append(str(int(value)))
    for candidate in candidates:
        if candidate in maps.get(field, {}):
            return maps[field][candidate]
    return None


def normalize_municipality(value: str) -> str:
    value = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()
    value = unicodedata.normalize("NFKD", value)
    return "".join(char for char in value if not unicodedata.combining(char)).casefold()


def main() -> None:
    code_to_download: dict[str, tuple[str, str]] = {}
    for filename, (expected_municipality, codes) in EXPECTED_FILES.items():
        for code in codes:
            code_to_download[code] = (filename, expected_municipality)

    codes = sorted(code_to_download)
    definition_bytes, layer_definition = get_json(SERVICE, {"f": "json"})
    definition_path = RAW_DIR / f"ideam_station_catalog_layer_definition_{ACCESS_DATE}.json"
    preserve_raw(definition_path, definition_bytes)

    query_payloads: list[dict] = []
    query_provenance: list[dict[str, str]] = []
    for code in codes:
        query_bytes, query = get_json(
            f"{SERVICE}/query",
            {
                "where": f"idestacion='{code}'",
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": "4326",
                "f": "json",
            },
        )
        query_path = RAW_DIR / f"ideam_station_catalog_{code}_{ACCESS_DATE}.json"
        preserve_raw(query_path, query_bytes)
        query_payloads.append(query)
        query_provenance.append(
            {
                "station_code": code,
                "path": str(query_path.relative_to(ROOT)).replace("\\", "/"),
                "sha256": sha256_bytes(query_bytes),
            }
        )

    domains = domain_maps(layer_definition)
    rows: list[dict[str, object]] = []
    returned_codes: set[str] = set()

    for query in query_payloads:
        for feature in query.get("features", []):
            attributes = feature.get("attributes", {})
            geometry = feature.get("geometry", {})
            code = str(attributes.get("idestacion") or "").strip()
            returned_codes.add(code)
            source_file, expected_municipality = code_to_download.get(code, (None, None))
            official_municipality = domain_value(domains, "idmunicipio", attributes.get("idmunicipio"))
            official_department = domain_value(domains, "iddepartamento", attributes.get("iddepartamento"))
            official_category = domain_value(domains, "idcategoria", attributes.get("idcategoria"))
            official_technology = domain_value(domains, "idtecnologiatm", attributes.get("idtecnologiatm"))
            official_status = domain_value(domains, "idestadoestaciontm", attributes.get("idestadoestaciontm"))

            rows.append(
            {
                "station_code": code,
                "station_name": attributes.get("nombre"),
                "source_csv": source_file,
                "download_label_municipality": expected_municipality,
                "official_department_code": attributes.get("iddepartamento"),
                "official_department": official_department,
                "official_municipality_code": attributes.get("idmunicipio"),
                "official_municipality": official_municipality,
                "download_label_matches_official_municipality": (
                    normalize_municipality(expected_municipality)
                    == normalize_municipality(official_municipality)
                    if expected_municipality and official_municipality
                    else None
                ),
                "category_code": attributes.get("idcategoria"),
                "category": official_category,
                "technology_code": attributes.get("idtecnologiatm"),
                "technology": official_technology,
                "status_code": attributes.get("idestadoestaciontm"),
                "status": official_status,
                "installation_date_epoch_ms": attributes.get("fechainstalacion"),
                "suspension_date_epoch_ms": attributes.get("fechasuspension"),
                "altitude_m": attributes.get("altitud"),
                "latitude_attribute": attributes.get("latitud"),
                "longitude_attribute": attributes.get("longitud"),
                "geometry_x_epsg4326": geometry.get("x"),
                "geometry_y_epsg4326": geometry.get("y"),
                "entity": domain_value(domains, "identidad", attributes.get("identidad")),
            }
        )

    rows.sort(key=lambda item: str(item["station_code"]))
    missing_codes = sorted(set(codes) - returned_codes)
    unexpected_codes = sorted(returned_codes - set(codes))
    matched_municipality = sum(
        item["download_label_matches_official_municipality"] is True for item in rows
    )
    mismatched_municipality = sum(
        item["download_label_matches_official_municipality"] is False for item in rows
    )

    INTERIM_DIR.mkdir(parents=True, exist_ok=True)
    output_csv = INTERIM_DIR / "ideam_precipitation_station_catalog_linkage.csv"
    if rows:
        with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    report = {
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "official_service": SERVICE,
        "raw_layer_definition": str(definition_path.relative_to(ROOT)).replace("\\", "/"),
        "raw_layer_definition_sha256": sha256_bytes(definition_bytes),
        "raw_query_responses": query_provenance,
        "requested_station_codes": codes,
        "requested_count": len(codes),
        "returned_count": len(rows),
        "missing_codes": missing_codes,
        "unexpected_codes": unexpected_codes,
        "download_label_official_municipality_matches": matched_municipality,
        "download_label_official_municipality_mismatches": mismatched_municipality,
        "rows_with_coordinates": sum(
            item["geometry_x_epsg4326"] is not None and item["geometry_y_epsg4326"] is not None
            for item in rows
        ),
        "rows_with_resolved_department": sum(item["official_department"] is not None for item in rows),
        "rows_with_resolved_municipality": sum(item["official_municipality"] is not None for item in rows),
        "rows_with_resolved_category": sum(item["category"] is not None for item in rows),
        "rows_with_resolved_status": sum(item["status"] is not None for item in rows),
        "interim_linkage_csv": str(output_csv.relative_to(ROOT)).replace("\\", "/"),
        "interpretation_limit": (
            "This is a station-code linkage to the official IDEAM station catalog. "
            "It verifies catalog metadata at the access date, not gauge calibration, "
            "observation completeness, measurement quality, or spatial representativeness."
        ),
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_json = RESULTS_DIR / "audit_ideam_precipitation_station_catalog_linkage.json"
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    for row in rows:
        print(
            row["station_code"],
            row["station_name"],
            row["official_municipality"],
            row["category"],
            row["status"],
            row["geometry_x_epsg4326"],
            row["geometry_y_epsg4326"],
            sep=" | ",
        )


if __name__ == "__main__":
    main()
