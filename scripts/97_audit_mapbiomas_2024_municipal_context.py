"""Audita la fracción agropecuaria municipal de MapBiomas para 2024.

Las celdas vacías no se convierten en cero por defecto. Solo se habilita una
codificación operativa de cero cuando el total numérico de las clases por
municipio concilia con el área geométrica independiente de Cortolima.
"""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import zipfile
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "raw" / "mapbiomas" / "mapbiomas_colombia_collection3_coverage_statistics.zip"
CROSSWALK = ROOT / "configs" / "ideam_cortolima_municipality_crosswalk.json"
MUNICIPAL_AREAS = ROOT / "data" / "interim" / "gwis_nrt_burned_area_municipality_summary_august_2026.csv"
OUTPUT = ROOT / "data" / "interim" / "mapbiomas_2024_municipal_context_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_mapbiomas_2024_municipal_context_v1.json"
MEMBER = "Statistics-for-Website-MB-Cobertura-col3.xlsx"
SHEET = "COBERTURA_MUNICIPIO"
AGRICULTURAL_LEVEL_1 = "3. AREA AGROPECUARIA"
MIN_RECONCILIATION_RATIO = 0.99
MAX_RECONCILIATION_RATIO = 1.01


def normalized(value: object) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]+", "", text.upper())


def sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def mapped_cortolima_name(mapbiomas_name: str, mappings: dict[str, dict[str, str]], valid_names: set[str]) -> str:
    key = normalized(mapbiomas_name)
    if key in valid_names:
        return key
    if key in mappings:
        return normalized(mappings[key]["cortolima_municipality"])
    raise RuntimeError(f"Unresolved MapBiomas municipality: {mapbiomas_name}")


def main() -> None:
    for target in (OUTPUT, AUDIT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    municipal_areas: dict[str, float] = {}
    with MUNICIPAL_AREAS.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            municipal_areas[normalized(row["municipality"])] = float(row["municipality_area_ha"])
    if len(municipal_areas) != 47:
        raise RuntimeError(f"Expected 47 Cortolima municipalities, found {len(municipal_areas)}")
    mappings = json.loads(CROSSWALK.read_text(encoding="utf-8"))["mappings"]
    grouped: dict[str, dict[str, object]] = defaultdict(lambda: {
        "mapbiomas_names": set(),
        "numeric_area_ha": 0.0,
        "agricultural_area_ha": 0.0,
        "record_count": 0,
        "blank_2024_record_count": 0,
        "agricultural_blank_2024_record_count": 0,
    })
    with zipfile.ZipFile(SOURCE) as archive, archive.open(MEMBER) as binary:
        workbook = load_workbook(io.BytesIO(binary.read()), read_only=True, data_only=True)
        sheet = workbook[SHEET]
        rows = sheet.iter_rows(values_only=True)
        header = [str(value).strip() if value is not None else "" for value in next(rows)]
        indices = {name: header.index(name) for name in ("municipio", "departamento", "class_level_1", "2024")}
        for row in rows:
            if normalized(row[indices["departamento"]]) != "TOLIMA":
                continue
            raw_name = str(row[indices["municipio"]]).strip()
            municipality = mapped_cortolima_name(raw_name, mappings, set(municipal_areas))
            bucket = grouped[municipality]
            bucket["mapbiomas_names"].add(raw_name)
            bucket["record_count"] += 1
            level_1 = normalized(row[indices["class_level_1"]])
            value = row[indices["2024"]]
            if value is None:
                bucket["blank_2024_record_count"] += 1
                if level_1 == AGRICULTURAL_LEVEL_1:
                    bucket["agricultural_blank_2024_record_count"] += 1
                continue
            if not isinstance(value, (int, float)) or value < 0:
                raise RuntimeError(f"Unexpected 2024 area value for {raw_name}: {value!r}")
            bucket["numeric_area_ha"] += float(value)
            if level_1 == AGRICULTURAL_LEVEL_1:
                bucket["agricultural_area_ha"] += float(value)
        workbook.close()
    if set(grouped) != set(municipal_areas):
        raise RuntimeError("MapBiomas and Cortolima municipality sets do not match")
    output_rows = []
    reconciliation_failures = []
    for municipality in sorted(grouped):
        bucket = grouped[municipality]
        boundary_area = municipal_areas[municipality]
        numeric_area = float(bucket["numeric_area_ha"])
        ratio = numeric_area / boundary_area
        reconciles = MIN_RECONCILIATION_RATIO <= ratio <= MAX_RECONCILIATION_RATIO
        if not reconciles:
            reconciliation_failures.append(municipality)
        output_rows.append({
            "municipality": municipality,
            "mapbiomas_municipality": "|".join(sorted(bucket["mapbiomas_names"])),
            "mapbiomas_numeric_class_area_ha_2024": round(numeric_area, 6),
            "cortolima_boundary_area_ha": boundary_area,
            "mapbiomas_to_boundary_area_ratio": ratio,
            "mapbiomas_agricultural_area_ha_2024": round(float(bucket["agricultural_area_ha"]), 6),
            "mapbiomas_agricultural_fraction_2024": float(bucket["agricultural_area_ha"]) / numeric_area if numeric_area else "",
            "mapbiomas_record_count": bucket["record_count"],
            "mapbiomas_blank_2024_record_count": bucket["blank_2024_record_count"],
            "mapbiomas_agricultural_blank_2024_record_count": bucket["agricultural_blank_2024_record_count"],
            "area_reconciliation_within_1_percent": reconciles,
        })
    with OUTPUT.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)
    blank_as_zero_operationally_supported = not reconciliation_failures
    audit = {
        "source": {"file": str(SOURCE.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(SOURCE), "member": MEMBER, "sheet": SHEET},
        "year": 2024,
        "unit": "municipality level class area in hectares",
        "municipality_count": len(output_rows),
        "blank_2024_record_count": sum(int(row["mapbiomas_blank_2024_record_count"]) for row in output_rows),
        "agricultural_blank_2024_record_count": sum(int(row["mapbiomas_agricultural_blank_2024_record_count"]) for row in output_rows),
        "area_reconciliation_rule": f"numeric class total divided by Cortolima boundary area must be between {MIN_RECONCILIATION_RATIO} and {MAX_RECONCILIATION_RATIO}",
        "municipalities_failing_area_reconciliation": reconciliation_failures,
        "blank_as_zero_operationally_supported_by_independent_area_reconciliation": blank_as_zero_operationally_supported,
        "interpretation": "A passing reconciliation supports treating omitted class areas operationally as zero for the municipal fraction. It does not establish the coverage class at the event anchor, observed use in 2026, land tenure, agricultural practice, ignition, or cause.",
        "documentation_limit": "The public MapBiomas statistics page confirms municipal areas in hectares from 1985 through 2024. It did not provide a cell-level definition of blank values during this audit. The output therefore retains blank counts and conditions use on reconciliation.",
        "output": {"file": str(OUTPUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(OUTPUT)},
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "municipality_count": audit["municipality_count"],
        "blank_2024_record_count": audit["blank_2024_record_count"],
        "agricultural_blank_2024_record_count": audit["agricultural_blank_2024_record_count"],
        "reconciliation_failures": reconciliation_failures,
        "blank_as_zero_operationally_supported": blank_as_zero_operationally_supported,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
