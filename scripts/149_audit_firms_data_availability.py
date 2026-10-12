"""Consulta la disponibilidad declarada por FIRMS para cada fuente de detección.

Responde dos preguntas abiertas del registro de decisiones del proyecto.

Primera, hasta qué fecha llega hoy el procesamiento estándar, lo que permite
proyectar cuándo estará disponible agosto de 2026 sin suponer una latencia.

Segunda, si existe una fuente de procesamiento estándar para NOAA 20, dado que la
auditoría interna del 7 de septiembre registró filas bajo esa etiqueta mientras la
documentación consultada el 9 de septiembre afirma que no existe.

La clave se lee del entorno o de `.env`, nunca se imprime y nunca se escribe en la
salida cruda, el manifiesto ni la auditoría.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
RAW_DIR = ROOT / "data" / "raw" / "firms_availability_2026-09-09"
AUDIT = ROOT / "results" / "exploratory" / "audit_firms_data_availability_2026-09-09.json"
ENDPOINT = "https://firms.modaps.eosdis.nasa.gov/api/data_availability/csv/{key}/all"
TIMEOUT = 120


def read_map_key() -> str:
    key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    if key:
        return key
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "FIRMS_MAP_KEY":
                return value.strip()
    raise RuntimeError("No se encontró FIRMS_MAP_KEY en el entorno ni en .env")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def main() -> None:
    key = read_map_key()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    target = RAW_DIR / "firms_data_availability_all.csv"
    status: object
    try:
        with urllib.request.urlopen(ENDPOINT.format(key=key), timeout=TIMEOUT) as response:
            status = response.status
            payload = response.read()
    except HTTPError as error:
        status = error.code
        payload = b""
    except URLError as error:
        raise RuntimeError("La conexión con FIRMS falló") from error

    if payload:
        target.write_bytes(payload)

    rows = []
    if payload:
        lines = payload.decode("utf-8").strip().splitlines()
        header = [name.strip() for name in lines[0].split(",")]
        for line in lines[1:]:
            values = [value.strip() for value in line.split(",")]
            rows.append(dict(zip(header, values)))

    standard_sources = [row for row in rows if row.get("data_id", "").upper().endswith("_SP")]
    noaa20 = [row for row in rows if "NOAA20" in row.get("data_id", "").upper()]

    audit = {
        "script": Path(__file__).name,
        "executed": date.today().isoformat(),
        "endpoint": ENDPOINT.format(key="REDACTADO"),
        "credential_handling": "La clave se leyó del entorno y no se registra en esta salida.",
        "http_status": status,
        "sources_returned": len(rows),
        "all_sources": rows,
        "standard_processing_sources": [row.get("data_id") for row in standard_sources],
        "noaa20_entries": noaa20,
        "raw_output": str(target.relative_to(ROOT)) if payload else None,
        "raw_output_sha256": sha256(target) if payload else None,
        "interpretation_limits": [
            "La respuesta describe lo que el servicio declara disponible en la fecha de consulta.",
            "No demuestra completitud, calidad ni equivalencia entre niveles de procesamiento.",
        ],
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"http_status": status, "sources_returned": len(rows), "rows": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
