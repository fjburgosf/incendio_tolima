"""Verify a preselected set of recent literature relevant to the Tolima fire study."""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "literature_crossref_2026-09-09"
OUTPUT = ROOT / "results" / "exploratory" / "verify_recent_literature_2026-09-09.json"
API = "https://api.crossref.org/works/{}"

# Candidate DOI and a title fragment fixed before requests are issued.
CANDIDATES = [
    ("10.1016/j.mex.2022.101741", "Agricultural burned area detection integrated approach Sentinel 2"),
    ("10.1016/j.jag.2023.103350", "Refining historical burned area data satellite observations"),
    ("10.3390/rs14133122", "Evaluating relative phenological correction Sentinel fire severity burned area"),
    ("10.1007/s11069-024-06743-6", "Fire activity and fire weather Lower Mekong"),
    ("10.1016/j.jag.2025.104889", "S2BAVG global Sentinel 2 grid burned area product validation"),
    ("10.18257/raccefyn.1514", "Cambios patrones espaciales área quemada Colombia"),
    ("10.1038/s43247-024-01406-7", "Drought terrestrial gross primary production exacerbated wildfires"),
    ("10.1088/1748-9326/adc8ba", "Paramo Fire Atlas burned area trends Tropical Andes"),
    ("10.1590/0001-3765202620240622", "Fire South America wetlands temporal spatial patterns"),
    ("10.1016/j.ecoinf.2023.102304", "Remote sensing delineation wildfire spatial extents post fire recovery"),
    ("10.1080/15481603.2022.2143690", "Near real time mapping burned area synergizing multiple satellites"),
    ("10.1071/WF22048", "Consistent high accuracy mapping daily sub daily wildfire growth satellite"),
    ("10.1038/s41893-026-01793-z", "Fire dynamics South American Chaco agriculture drought"),
    ("10.1088/1748-9326/ad928f", "weather pattern responsible increasing wildfires western United States"),
]


def tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    return {token for token in re.findall(r"[a-z0-9]+", normalized) if len(token) > 2}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite {OUTPUT}")
    RAW.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "TolimaFireLiteratureAudit/1.0 research contact unavailable"})
    records = []
    for doi, expected in CANDIDATES:
        destination = RAW / (re.sub(r"[^A-Za-z0-9._-]+", "_", doi) + ".json")
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite {destination}")
        response = session.get(API.format(doi), timeout=45)
        response.raise_for_status()
        destination.write_bytes(response.content)
        work = response.json()["message"]
        title = " ".join(work.get("title", []))
        expected_tokens = tokens(expected)
        overlap = len(expected_tokens & tokens(title)) / len(expected_tokens)
        year_parts = work.get("published", {}).get("date-parts", [[None]])
        year = year_parts[0][0] if year_parts and year_parts[0] else None
        record = {
            "doi_requested": doi,
            "doi_returned": work.get("DOI", ""),
            "title": title,
            "title_overlap": overlap,
            "year": year,
            "type": work.get("type"),
            "container": " ".join(work.get("container-title", [])),
            "authors": work.get("author", []),
            "volume": work.get("volume", ""),
            "issue": work.get("issue", ""),
            "page": work.get("page", ""),
            "article_number": work.get("article-number", ""),
            "url": work.get("URL", ""),
            "raw_file": str(destination.relative_to(ROOT)),
            "raw_sha256": sha256(destination),
            "accept": work.get("DOI", "").casefold() == doi.casefold() and overlap >= 0.40 and year is not None and year >= 2022,
        }
        records.append(record)
        time.sleep(0.2)
    result = {
        "audit_date": "2026-09-09",
        "acceptance_rule": "exact DOI, title overlap at least 0.40 and publication year from 2022 onward",
        "candidate_count": len(records),
        "accepted_count": sum(record["accept"] for record in records),
        "records": records,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if result["accepted_count"] != result["candidate_count"]:
        raise RuntimeError("At least one literature record failed the frozen verification gate")
    print(json.dumps({key: result[key] for key in ("candidate_count", "accepted_count")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
