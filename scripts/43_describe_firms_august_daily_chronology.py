"""Describe cronología NRT de FIRMS sin convertir detecciones en incendios."""

from __future__ import annotations

import csv
import importlib.util
import json
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIRMS_DIRECTORY = ROOT / "data" / "raw" / "firms"
MANIFEST = FIRMS_DIRECTORY / "download_manifest_august_2026.json"
LINKAGE_SCRIPT = ROOT / "scripts" / "06_audit_firms_municipal_linkage.py"
OUTPUT = ROOT / "results" / "exploratory" / "describe_firms_august_daily_chronology.json"


def load_linkage_helpers():
    specification = importlib.util.spec_from_file_location("firms_linkage", LINKAGE_SCRIPT)
    if specification is None or specification.loader is None:
        raise RuntimeError("No se pudo cargar el control de enlace FIRMS")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def matching_municipality(point: tuple[float, float], municipalities: list[dict], contains) -> str | None:
    candidates = []
    for municipality in municipalities:
        west, south, east, north = municipality["bbox"]
        if west <= point[0] <= east and south <= point[1] <= north and contains(point, municipality["geometry"]):
            candidates.append(municipality["name"])
    if len(candidates) == 1:
        return candidates[0]
    return None


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("La cronología FIRMS ya existe. No se sobreescriben resultados.")
    linkage = load_linkage_helpers()
    municipalities = linkage.build_municipal_index()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    daily_source_counts: dict[str, Counter[str]] = defaultdict(Counter)
    daily_municipal_counts: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    daily_unmatched: dict[str, Counter[str]] = defaultdict(Counter)
    for entry in manifest["entries"]:
        source = entry["source"]
        with (FIRMS_DIRECTORY / entry["file"]).open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                day = row["acq_date"]
                daily_source_counts[source][day] += 1
                municipality = matching_municipality((float(row["longitude"]), float(row["latitude"])), municipalities, linkage.geometry_contains)
                if municipality is None:
                    daily_unmatched[source][day] += 1
                else:
                    daily_municipal_counts[source][(day, municipality)] += 1
    dates = sorted({date for counts in daily_source_counts.values() for date in counts})
    daily = []
    for day in dates:
        source_counts = {source: daily_source_counts[source][day] for source in sorted(daily_source_counts)}
        matched_counts = {source: sum(count for (date, _), count in daily_municipal_counts[source].items() if date == day) for source in sorted(daily_source_counts)}
        daily.append({"date": day, "detections_by_source": source_counts, "single_municipal_matches_by_source": matched_counts, "no_municipal_match_by_source": {source: daily_unmatched[source][day] for source in sorted(daily_source_counts)}})
    top_cells = []
    for source, cells in sorted(daily_municipal_counts.items()):
        for (day, municipality), count in cells.most_common(20):
            top_cells.append({"source": source, "date": day, "municipality": municipality, "detection_count": count})
    report = {
        "input_status": manifest["data_status"],
        "source": "FIRMS VIIRS NRT",
        "daily": daily,
        "top_municipality_day_detection_counts_by_source": top_cells,
        "interpretation_limit": "Describe detecciones térmicas NRT por fecha, fuente y enlace municipal único. No confirma incendio de vegetación, inicio, área quemada, severidad, duración o ausencia de incendio en otros días.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"calendar_day_count": len(daily), "top_municipality_day_detection_counts_by_source": top_cells[:10]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
