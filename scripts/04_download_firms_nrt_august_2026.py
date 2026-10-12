"""Descarga detecciones FIRMS NRT por intervalos de cinco días.

La clave se recibe solo como argumento de ejecución. No se guarda en archivos,
mensajes de salida ni manifiestos. Los CSV se conservan sin transformación.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "data" / "raw" / "firms"
MANIFEST = DESTINATION / "download_manifest_august_2026.json"
BBOX = "-76.2,2.0,-74.0,6.2"
CHUNKS = (
    ("2026-08-01", 5),
    ("2026-08-06", 5),
    ("2026-08-11", 5),
    ("2026-08-16", 5),
    ("2026-08-21", 5),
    ("2026-08-26", 5),
    ("2026-08-31", 1),
)
SOURCES = ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT")
API_TEMPLATE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{bbox}/{days}/{start}"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map-key", required=True, help="MAP_KEY de FIRMS, no se almacena")
    return parser.parse_args()


def validate_csv(payload: bytes) -> tuple[int, list[str]]:
    text = payload.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("FIRMS devolvió una respuesta vacía")
    header = rows[0]
    required = {"latitude", "longitude", "acq_date", "acq_time", "frp"}
    if not required.issubset(header):
        raise ValueError("La respuesta no contiene el esquema esperado de FIRMS")
    return len(rows) - 1, header


def fetch(url: str) -> bytes:
    try:
        with urlopen(url, timeout=120) as response:
            return response.read()
    except HTTPError as error:
        raise RuntimeError(f"FIRMS devolvió HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError("No fue posible conectar con FIRMS") from error


def main() -> None:
    arguments = parse_arguments()
    DESTINATION.mkdir(parents=True, exist_ok=True)
    planned = [
        DESTINATION / f"{source.lower()}_{start}_{days}d.csv"
        for source in SOURCES
        for start, days in CHUNKS
    ]
    existing = [path for path in planned if path.exists()]
    if existing or MANIFEST.exists():
        names = ", ".join(path.name for path in existing + ([MANIFEST] if MANIFEST.exists() else []))
        raise FileExistsError(f"Se protege el dato crudo existente. No se sobrescribirá {names}")

    entries = []
    for source in SOURCES:
        for start, days in CHUNKS:
            filename = f"{source.lower()}_{start}_{days}d.csv"
            target = DESTINATION / filename
            url = API_TEMPLATE.format(key=arguments.map_key, source=source, bbox=BBOX, days=days, start=start)
            payload = fetch(url)
            row_count, fields = validate_csv(payload)
            target.write_bytes(payload)
            entries.append(
                {
                    "file": filename,
                    "source": source,
                    "bbox": BBOX,
                    "start_date": start,
                    "day_range": days,
                    "record_count": row_count,
                    "fields": fields,
                    "sha256": hashlib.sha256(payload).hexdigest().upper(),
                    "request_template": (
                        "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
                        "[REDACTED_MAP_KEY]/{source}/{bbox}/{days}/{start}"
                    ).format(source=source, bbox=BBOX, days=days, start=start),
                }
            )
            print(f"Descargado {filename} con {row_count} detecciones")

    manifest = {
        "downloaded_at_utc": datetime.now(UTC).isoformat(),
        "data_status": "Near Real Time, no sustituye Standard Processing",
        "entries": entries,
        "interpretation_limit": (
            "Las detecciones son puntos de actividad térmica. No delimitan un incendio, "
            "un perímetro ni un área quemada."
        ),
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Manifiesto creado con {len(entries)} archivos y sin MAP_KEY")


if __name__ == "__main__":
    main()
