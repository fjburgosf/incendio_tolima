"""Descarga e inspecciona la estadística municipal de cobertura MapBiomas Colombia."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections import Counter
from pathlib import Path
from urllib.request import urlopen

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
URL = "https://storage.googleapis.com/mapbiomas-public/initiatives/colombia/collection_3/statistics/Statistics-for-Website-MB-Cobertura-col3.zip"
RAW_DIR = ROOT / "data" / "raw" / "mapbiomas"
OUTPUT = RAW_DIR / "mapbiomas_colombia_collection3_coverage_statistics.zip"
MANIFEST = RAW_DIR / "mapbiomas_colombia_collection3_coverage_statistics_manifest.json"
AUDIT = ROOT / "results" / "exploratory" / "audit_mapbiomas_colombia_coverage_statistics.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("La auditoría de estadísticas MapBiomas ya existe. No se sobreescriben insumos ni resultados.")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if OUTPUT.exists():
        content_type = "application/zip"
    else:
        with urlopen(URL, timeout=240) as response:
            payload = response.read()
            content_type = response.headers.get_content_type()
        OUTPUT.write_bytes(payload)
    manifest = {"url": URL, "file": OUTPUT.name, "bytes": OUTPUT.stat().st_size, "sha256": sha256(OUTPUT), "content_type": content_type}
    if not MANIFEST.exists():
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(OUTPUT) as archive:
        members = archive.namelist()
        workbook_members = [name for name in members if name.lower().endswith(".xlsx")]
        if not workbook_members:
            raise ValueError("El ZIP no contiene un libro XLSX")
        summaries = []
        for member in workbook_members:
            with archive.open(member) as binary:
                workbook = load_workbook(io.BytesIO(binary.read()), read_only=True, data_only=True)
                for sheet in workbook.worksheets:
                    rows = sheet.iter_rows(values_only=True)
                    header = [str(value).strip() if value is not None else "" for value in next(rows)]
                    normalized = {value.upper(): position for position, value in enumerate(header)}
                    total = 0
                    tolima = 0
                    years: Counter[str] = Counter()
                    municipalities: set[str] = set()
                    dept_index = normalized.get("DEPARTAMENTO")
                    year_index = normalized.get("YEAR", normalized.get("AÑO"))
                    municipality_index = normalized.get("MUNICIPIO")
                    for row in rows:
                        if not any(value is not None for value in row):
                            continue
                        total += 1
                        department = str(row[dept_index]).strip().upper() if dept_index is not None else ""
                        if department == "TOLIMA":
                            tolima += 1
                            if municipality_index is not None:
                                municipalities.add(str(row[municipality_index]).strip())
                            if year_index is not None:
                                years[str(row[year_index]).strip()] += 1
                    summaries.append({"member": member, "sheet": sheet.title, "fields": header, "total_records": total, "tolima_record_count": tolima, "tolima_unique_municipality_count": len(municipalities), "tolima_records_by_year": dict(sorted(years.items()))})
                workbook.close()
    audit = {
        "source_file": str(OUTPUT.relative_to(ROOT)).replace("\\", "/"),
        "zip_members": members,
        "csv_summaries": summaries,
        "interpretation_limit": "Las estadísticas describen áreas de cobertura mapeadas por MapBiomas, no observaciones de campo, predios, actividad de quema o causalidad.",
    }
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"bytes": manifest["bytes"], "sha256": manifest["sha256"], "csv_summaries": summaries}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
