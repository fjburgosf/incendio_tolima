"""Verify candidate literature DOIs against Crossref and freeze the responses."""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "literature_crossref_2026-09-06"
OUTPUT = ROOT / "results" / "exploratory" / "verify_literature_dois_2026-09-06.json"
API = "https://api.crossref.org/works/{doi}"

# Expected title fragments are frozen before querying and are used to catch a
# valid DOI that belongs to a different publication.
CANDIDATES = [
    ("10.1016/j.rse.2013.12.008", "VIIRS 375 m active fire detection"),
    ("10.1016/j.rse.2015.01.010", "VIIRS 375 m active fire detection product for direct burned area mapping"),
    ("10.5194/egusphere-2025-1823", "near real time global burned area"),
    ("10.1038/s41597-019-0312-2", "global wildfire dataset"),
    ("10.1016/j.rse.2018.08.005", "Collection 6 MODIS burned area mapping algorithm"),
    ("10.1016/j.rse.2019.111490", "global validation Collection 6 MODIS burned area"),
    ("10.1016/j.rse.2018.12.011", "Sentinel 2 burned area algorithm"),
    ("10.5194/essd-13-5353-2021", "refined burned area mapping protocol Sentinel 2"),
    ("10.3390/rs16071166", "Sentinel 2 reference fire perimeters Latin America Caribbean"),
    ("10.1175/2010EI344.1", "MODIS reflectance active fire data burn mapping Colombia"),
    ("10.1016/j.rse.2014.02.015", "good practices estimating area assessing accuracy land change"),
    ("10.1016/j.rse.2016.09.016", "stratified random sampling space time burned area validation"),
    ("10.3390/fire8060221", "wildfires Magdalena Colombia precipitation"),
    ("10.1071/WF20030", "fire history Iguaque Colombia"),
    ("10.1111/gcb.12538", "fire tropical Andes climate"),
    ("10.1002/2013GB004664", "fire tropical Andean landscapes"),
    ("10.1071/WF21029", "wildfire environmental conditions Peruvian Andes"),
    ("10.4996/fireecology.130288764", "fire Huascaran Peru"),
    ("10.1080/19475705.2025.2605112", "wildfire occurrence extent high Andean grasslands"),
    ("10.1002/ecs2.2171", "climate variability fire Chile"),
    ("10.1038/s41467-017-02771-y", "Amazonian fire drought deforestation"),
    ("10.1088/1748-9326/ac462e", "Pantanal 2020 drought heat fire"),
    ("10.1088/1748-9326/ac5fa1", "global fire weather burned area"),
    ("10.1029/2023EF004334", "antecedent concurrent climate burned area ecoregions"),
    ("10.1038/ncomms8537", "global wildfire danger season length"),
    ("10.1029/2020JD033180", "meteorological factors wildfire size California"),
    ("10.1029/2024GL112539", "weather wildfire duration western United States"),
]


def normalize(value: str) -> set[str]:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    return {token for token in re.findall(r"[a-z0-9]+", value) if len(token) > 2}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {OUTPUT}")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "TolimaFireLiteratureAudit/1.0 (research DOI verification)"})
    records = []
    for doi, expected in CANDIDATES:
        raw_path = RAW_DIR / (re.sub(r"[^A-Za-z0-9._-]+", "_", doi) + ".json")
        if raw_path.exists():
            raise FileExistsError(f"Refusing to overwrite existing Crossref response: {raw_path}")
        response = session.get(API.format(doi=doi), timeout=45)
        response.raise_for_status()
        raw_path.write_bytes(response.content)
        payload = response.json()["message"]
        returned_doi = payload.get("DOI", "")
        title = " ".join(payload.get("title", []))
        expected_tokens = normalize(expected)
        title_tokens = normalize(title)
        coverage = len(expected_tokens & title_tokens) / len(expected_tokens) if expected_tokens else 0.0
        records.append({
            "candidate_doi": doi,
            "returned_doi": returned_doi,
            "doi_exact_match": returned_doi.casefold() == doi.casefold(),
            "expected_title_fragment": expected,
            "returned_title": title,
            "expected_token_coverage": coverage,
            "title_match_gate": coverage >= 0.45,
            "type": payload.get("type"),
            "container_title": " ".join(payload.get("container-title", [])),
            "published": payload.get("published", {}).get("date-parts"),
            "author": payload.get("author", []),
            "publisher": payload.get("publisher"),
            "url": payload.get("URL"),
            "raw_file": str(raw_path.relative_to(ROOT)),
            "raw_sha256": sha256(raw_path),
        })
        time.sleep(0.2)

    failures = [r for r in records if not (r["doi_exact_match"] and r["title_match_gate"])]
    result = {
        "audit_date": "2026-09-06",
        "source": "Crossref REST API",
        "acceptance_rule_frozen_before_execution": "Exact DOI match and at least 0.45 coverage of expected title tokens",
        "candidate_count": len(records),
        "verified_count": len(records) - len(failures),
        "failure_count": len(failures),
        "all_candidates_verified": not failures,
        "records": records,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("candidate_count", "verified_count", "failure_count", "all_candidates_verified")}, indent=2))
    if failures:
        raise RuntimeError(f"DOI verification gate failed for {len(failures)} candidate(s)")


if __name__ == "__main__":
    main()
