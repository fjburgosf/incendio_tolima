"""Audit public CMR availability of historic burned-area granules for Tolima."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
ACCESS_DATE = "2026-09-07"
BASE_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"
BBOX = "-76.2,2.0,-74.0,6.2"
YEARS = (2018, 2020, 2022, 2024, 2025, 2026)
PRODUCTS = (("MCD64A1", "061"), ("VNP64A1", "002"))
OUTPUT = ROOT / "results" / "exploratory" / f"audit_historical_nasa_burned_area_catalogue_{ACCESS_DATE}_v3.json"


def query(short_name: str, version: str, year: int) -> dict[str, object]:
    params = {
        "short_name": short_name,
        "version": version,
        # Request broadly enough to reveal boundary overlaps, then retain only
        # a product whose own coverage begins in the target August.
        "temporal": f"{year}-08-01T00:00:00Z,{year}-09-03T00:00:00Z",
        "bounding_box": BBOX,
        "page_size": 200,
        "sort_key": "start_date",
    }
    request = Request(
        f"{BASE_URL}?{urlencode(params)}",
        headers={"Accept": "application/json", "User-Agent": "tolima-fire-data-audit/1.0"},
    )
    with urlopen(request, timeout=90) as response:
        payload = json.load(response)
    entries = payload.get("feed", {}).get("entry", [])
    eligible_entries = [
        item for item in entries
        if str(item.get("time_start", "")).startswith(f"{year}-08-")
    ]
    return {
        "short_name": short_name,
        "version": version,
        "year": year,
        "temporal": params["temporal"],
        "bounding_box": BBOX,
        "catalogue_entry_count_before_start_date_filter": len(entries),
        "eligible_entry_count": len(eligible_entries),
        "entries": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "time_start": item.get("time_start"),
                "time_end": item.get("time_end"),
                "updated": item.get("updated"),
                "data_center": item.get("data_center"),
            }
            for item in eligible_entries
        ],
    }


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {OUTPUT}")
    queries = [query(product, version, year) for product, version in PRODUCTS for year in YEARS]
    output = {
        "audit_date_utc": datetime.now(timezone.utc).isoformat(),
        "inference_class": "descriptive catalogue audit",
        "queries": queries,
        "interpretation": (
            "A CMR catalogue entry establishes only that metadata for a granule intersected "
            "the query. It does not establish authenticated download, pixel values, quality, "
            "burn-date validity, complete Tolima coverage, or independence from active-fire inputs."
        ),
    }
    serialized = json.dumps(output, ensure_ascii=False, indent=2)
    OUTPUT.write_text(serialized, encoding="utf-8")
    print(f"output={OUTPUT.relative_to(ROOT)}")
    print(f"sha256={hashlib.sha256(serialized.encode('utf-8')).hexdigest().upper()}")
    for item in queries:
        print(f"{item['short_name']} {item['year']}: {item['eligible_entry_count']} eligible entries")


if __name__ == "__main__":
    main()
