"""Verify the three additional recent research articles needed for the literature quota."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "literature_crossref_2026-09-09_additional"
OUTPUT = ROOT / "results" / "exploratory" / "verify_additional_recent_research_2026-09-09.json"
CANDIDATES = [
    ("10.1016/j.rse.2025.115006", "NASA VIIRS burned area product global validation intercomparison MODIS"),
    ("10.1016/j.ecolind.2025.114501", "REBAI development validation indicator burned area Sentinel 2"),
    ("10.1016/j.scitotenv.2021.152002", "socio economic land cover drivers wildfire activity south central Chile"),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest().upper()
    return digest


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite {OUTPUT}")
    RAW.mkdir(parents=True, exist_ok=True)
    records = []
    for doi, expected in CANDIDATES:
        destination = RAW / (re.sub(r"[^A-Za-z0-9._-]+", "_", doi) + ".json")
        response = requests.get(
            "https://api.crossref.org/works/" + doi,
            headers={"User-Agent": "TolimaFireLiteratureAudit/1.0 research contact unavailable"},
            timeout=45,
        )
        response.raise_for_status()
        destination.write_bytes(response.content)
        work = response.json()["message"]
        title = " ".join(work.get("title", []))
        year = work.get("published", {}).get("date-parts", [[None]])[0][0]
        expected_tokens = set(re.findall(r"[a-z0-9]+", expected.lower()))
        observed_tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
        overlap = len(expected_tokens & observed_tokens) / len(expected_tokens)
        record = {
            "doi_requested": doi,
            "doi_returned": work.get("DOI", ""),
            "title": title,
            "year": year,
            "type": work.get("type"),
            "container": " ".join(work.get("container-title", [])),
            "authors": work.get("author", []),
            "volume": work.get("volume", ""),
            "issue": work.get("issue", ""),
            "page": work.get("page", "") or work.get("article-number", ""),
            "url": work.get("URL", ""),
            "title_overlap": overlap,
            "raw_file": str(destination.relative_to(ROOT)),
            "raw_sha256": sha256(destination),
        }
        record["accept"] = (
            record["doi_returned"].casefold() == doi.casefold()
            and record["type"] == "journal-article"
            and year >= 2022
            and overlap >= 0.40
        )
        records.append(record)
    result = {"audit_date": "2026-09-09", "candidate_count": len(records), "accepted_count": sum(r["accept"] for r in records), "records": records}
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if result["accepted_count"] != result["candidate_count"]:
        raise RuntimeError("Additional article verification failed")
    print(json.dumps({"candidate_count": result["candidate_count"], "accepted_count": result["accepted_count"]}))


if __name__ == "__main__":
    main()
