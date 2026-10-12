"""Audita archivos crudos FIRMS NRT descargados para agosto de 2026.

No modifica CSV crudos. Los conteos son controles de estructura y no medidas de
incendios, área quemada, severidad ni eventos independientes.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RAW_DIRECTORY = ROOT / "data" / "raw" / "firms"
MANIFEST = RAW_DIRECTORY / "download_manifest_august_2026.json"
OUTPUT = ROOT / "results" / "exploratory" / "audit_firms_nrt_august_2026.json"
REQUIRED_FIELDS = {"latitude", "longitude", "acq_date", "acq_time", "confidence", "frp"}
WEST, SOUTH, EAST, NORTH = -76.2, 2.0, -74.0, 6.2


def record_key(row: dict[str, str], fieldnames: list[str]) -> tuple[str, ...]:
    return tuple(row.get(field, "") for field in fieldnames)


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    by_source: dict[str, dict] = defaultdict(lambda: {
        "record_count": 0,
        "files": [],
        "field_sets": [],
        "dates": [],
        "missing_required": Counter(),
        "out_of_bbox_count": 0,
        "exact_duplicate_count": 0,
        "categorical_values": {
            "satellite": Counter(),
            "instrument": Counter(),
            "confidence": Counter(),
            "version": Counter(),
            "daynight": Counter(),
        },
        "latitude_values": [],
        "longitude_values": [],
    })
    all_rows = 0

    for entry in manifest["entries"]:
        source = entry["source"]
        path = RAW_DIRECTORY / entry["file"]
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            rows = list(reader)
        observed_count = len(rows)
        if observed_count != entry["record_count"]:
            raise ValueError(f"El recuento cambió para {path.name}")
        summary = by_source[source]
        summary["record_count"] += observed_count
        summary["files"].append(path.name)
        summary["field_sets"].append(fields)
        seen: set[tuple[str, ...]] = set()
        for row in rows:
            all_rows += 1
            for field in REQUIRED_FIELDS:
                if not row.get(field, "").strip():
                    summary["missing_required"][field] += 1
            try:
                latitude = float(row["latitude"])
                longitude = float(row["longitude"])
                summary["latitude_values"].append(latitude)
                summary["longitude_values"].append(longitude)
                if not (WEST <= longitude <= EAST and SOUTH <= latitude <= NORTH):
                    summary["out_of_bbox_count"] += 1
            except (KeyError, ValueError):
                summary["out_of_bbox_count"] += 1
            try:
                summary["dates"].append(date.fromisoformat(row["acq_date"]).isoformat())
            except (KeyError, ValueError):
                pass
            key = record_key(row, fields)
            if key in seen:
                summary["exact_duplicate_count"] += 1
            else:
                seen.add(key)
            for field, counts in summary["categorical_values"].items():
                counts[row.get(field, "")] += 1

    report_sources = {}
    for source, summary in sorted(by_source.items()):
        field_sets = {tuple(fields) for fields in summary.pop("field_sets")}
        report_sources[source] = {
            "record_count": summary["record_count"],
            "file_count": len(summary["files"]),
            "files": sorted(summary["files"]),
            "field_sets": [list(fields) for fields in sorted(field_sets)],
            "required_fields_present_in_every_file": all(
                REQUIRED_FIELDS.issubset(set(fields)) for fields in field_sets
            ),
            "min_acq_date": min(summary["dates"]) if summary["dates"] else None,
            "max_acq_date": max(summary["dates"]) if summary["dates"] else None,
            "missing_required_field_values": dict(sorted(summary["missing_required"].items())),
            "out_of_bbox_count": summary["out_of_bbox_count"],
            "exact_duplicate_count_within_source": summary["exact_duplicate_count"],
            "latitude_range": [min(summary["latitude_values"]), max(summary["latitude_values"])],
            "longitude_range": [min(summary["longitude_values"]), max(summary["longitude_values"])],
            "observed_categorical_values": {
                field: dict(sorted(counts.items()))
                for field, counts in summary["categorical_values"].items()
            },
        }

    audit = {
        "input_manifest": str(MANIFEST.relative_to(ROOT)).replace("\\", "/"),
        "input_status": manifest["data_status"],
        "expected_bbox": {"west": WEST, "south": SOUTH, "east": EAST, "north": NORTH},
        "source_count": len(report_sources),
        "total_record_count": all_rows,
        "sources": report_sources,
        "interpretation_limit": (
            "Los recuentos describen detecciones NRT de dos sensores dentro de un rectángulo "
            "de cribado. No estiman número de incendios, área quemada, severidad o causalidad."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
