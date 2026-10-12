"""Audit public CMR catalogue metadata for August 2026 burned-area products.

This makes no authenticated download and does not claim usable pixels.  It records
whether the Earthdata catalogue exposes product granules for the relevant month.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "results" / "exploratory" / "audit_earthdata_cmr_burned_area_august_2026.json"
BASE = "https://cmr.earthdata.nasa.gov/search/granules.json"
TEMPORAL = "2026-08-01T00:00:00Z,2026-09-01T00:00:00Z"


def get_entries(short_name: str, version: str) -> dict:
    params = {
        "short_name": short_name,
        "version": version,
        "temporal": TEMPORAL,
        "page_size": 200,
        "sort_key": "start_date",
    }
    url = f"{BASE}?{urlencode(params)}"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "tolima-fire-data-audit/1.0"})
    with urlopen(request, timeout=60) as response:
        payload = json.load(response)
    entries = payload.get("feed", {}).get("entry", [])
    concise = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "time_start": item.get("time_start"),
            "time_end": item.get("time_end"),
            "updated": item.get("updated"),
            "data_center": item.get("data_center"),
        }
        for item in entries
    ]
    return {"url": url, "count_returned": len(entries), "entries": concise}


def main() -> None:
    result = {
        "accessed_at_utc": datetime.now(timezone.utc).isoformat(),
        "temporal_query": TEMPORAL,
        "products": {
            "MCD64A1": get_entries("MCD64A1", "061"),
            "VNP64A1": get_entries("VNP64A1", "002"),
        },
        "interpretation_limit": (
            "CMR catalogue metadata proves neither successful Earthdata download, "
            "Tolima coverage, pixel validity, burn date accuracy, nor area burned."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    OUTPUT.write_text(serialized, encoding="utf-8")
    print(OUTPUT)
    print(f"sha256={hashlib.sha256(serialized.encode('utf-8')).hexdigest()}")
    for name, info in result["products"].items():
        print(f"{name}: {info['count_returned']} catalogue entries")


if __name__ == "__main__":
    main()
