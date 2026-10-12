"""Audita compatibilidad municipio día entre FIRMS NRT y ERA5 Land."""

from __future__ import annotations

import csv
import importlib.util
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
FIRMS_DIR = ROOT / "data" / "raw" / "firms"
GRID_LINKAGE = ROOT / "data" / "interim" / "era5_land_grid_municipality_linkage.csv"
OUTPUT = ROOT / "results" / "exploratory" / "audit_firms_era5_municipality_day_linkage.json"
FOCUS = ["CHAPARRAL", "COYAIMA", "GUAMO", "ORTEGA", "SAN LUIS"]


def load_linkage_module():
    path = ROOT / "scripts" / "06_audit_firms_municipal_linkage.py"
    spec = importlib.util.spec_from_file_location("firms_linkage", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribirá")
    linkage = load_linkage_module()
    municipalities = linkage.build_municipal_index()
    detections: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for path in sorted(FIRMS_DIR.glob("viirs_noaa*_nrt_2026-08-*.csv")):
        source = "NOAA20" if "noaa20" in path.name else "NOAA21"
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                point = (float(row["longitude"]), float(row["latitude"]))
                matches = [
                    municipality["name"]
                    for municipality in municipalities
                    if municipality["bbox"][0] <= point[0] <= municipality["bbox"][2]
                    and municipality["bbox"][1] <= point[1] <= municipality["bbox"][3]
                    and linkage.geometry_contains(point, municipality["geometry"])
                ]
                if len(matches) == 1 and matches[0] in FOCUS:
                    detections[matches[0]][source].add(row["acq_date"])
    with GRID_LINKAGE.open("r", encoding="utf-8-sig", newline="") as stream:
        grid_counts = defaultdict(int)
        for row in csv.DictReader(stream):
            if row["municipality_match_count"] == "1":
                grid_counts[row["municipality"]] += 1
    august_dates = {(date(2026, 8, 1) + timedelta(days=offset)).isoformat() for offset in range(31)}
    records = []
    for municipality in FOCUS:
        noaa20 = detections[municipality]["NOAA20"]
        noaa21 = detections[municipality]["NOAA21"]
        union = noaa20 | noaa21
        records.append(
            {
                "municipality": municipality,
                "era5_grid_centers": grid_counts[municipality],
                "era5_august_day_count": len(august_dates),
                "firms_noaa20_detection_day_count": len(noaa20),
                "firms_noaa21_detection_day_count": len(noaa21),
                "firms_any_source_detection_day_count": len(union),
                "municipality_days_with_era5_and_any_firms_detection": len(august_dates & union),
                "firms_any_source_detection_dates": sorted(union),
            }
        )
    result = {
        "linkage_type": ["geographic by municipality", "temporal by UTC calendar date"],
        "focus_municipalities": records,
        "interpretation_limit": "Prueba disponibilidad de llaves municipio y fecha. No combina sensores como observaciones independientes, no define días sin detección como ausencia de incendio y no crea una exposición meteorológica ni un desenlace.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
