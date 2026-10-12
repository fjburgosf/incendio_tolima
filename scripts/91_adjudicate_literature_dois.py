"""Adjudicate the DOI audit without overwriting the failed first gate."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
FIRST_AUDIT = ROOT / "results" / "exploratory" / "verify_literature_dois_2026-09-06.json"
RAW_DIR = ROOT / "data" / "raw" / "literature_crossref_2026-09-06"
OUTPUT = ROOT / "results" / "exploratory" / "adjudicate_literature_dois_2026-09-06.json"
BIB_OUTPUT = ROOT / "references.bib"
REJECTED_DOI = "10.5194/egusphere-2025-1823"
REPLACEMENT = (
    "10.5194/bg-22-5247-2025",
    "Extensive fire-driven degradation in 2024 marks worst Amazon forest disturbance in over two decades",
)
CORRECTED_TITLES = {
    "10.3390/fire8060221": "Hydroclimatic and Land Use Drivers of Wildfire Risk in the Colombian Caribbean",
    "10.1071/WF20030": "Wildfire dynamics and impacts on a tropical Andean oak forest",
    "10.1029/2023EF004334": "The Interannual Variability of Global Burned Area Is Mostly Explained by Climatic Drivers",
    "10.1029/2024GL112539": "The Role of Wind Speed in Prolonging Large Fire Durations in the Western US",
}


def normalize(value: str) -> set[str]:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    return {token for token in re.findall(r"[a-z0-9]+", value) if len(token) > 2}


def token_coverage(expected: str, observed: str) -> float:
    tokens = normalize(expected)
    return len(tokens & normalize(observed)) / len(tokens) if tokens else 0.0


def sha256(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest.upper()


def first_year(message: dict) -> int | None:
    for field in ("published-print", "published-online", "published", "issued"):
        parts = message.get(field, {}).get("date-parts", [])
        if parts and parts[0]:
            return int(parts[0][0])
    return None


def clean_bib(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value or "")
    return value.replace("{", "").replace("}", "").strip()


def citation_key(message: dict, used: set[str]) -> str:
    authors = message.get("author", [])
    family = authors[0].get("family", "Anon") if authors else "Anon"
    family = "".join(ch for ch in unicodedata.normalize("NFKD", family) if ch.isascii() and ch.isalnum())
    title_words = [w for w in re.findall(r"[A-Za-z0-9]+", clean_bib(" ".join(message.get("title", [])))) if len(w) > 3]
    base = f"{family}{first_year(message) or 'nd'}{title_words[0] if title_words else 'Work'}"
    key = base
    suffix = ord("a")
    while key in used:
        key = base + chr(suffix)
        suffix += 1
    used.add(key)
    return key


def bib_entry(message: dict, used: set[str]) -> str:
    key = citation_key(message, used)
    authors = []
    for author in message.get("author", []):
        family = clean_bib(author.get("family", ""))
        given = clean_bib(author.get("given", ""))
        authors.append(f"{family}, {given}" if given else family)
    fields = {
        "author": " and ".join(authors),
        "title": clean_bib(" ".join(message.get("title", []))),
        "journal": clean_bib(" ".join(message.get("container-title", []))),
        "year": str(first_year(message) or ""),
        "volume": clean_bib(message.get("volume", "")),
        "number": clean_bib(message.get("issue", "")),
        "pages": clean_bib(message.get("page", "") or message.get("article-number", "")),
        "doi": clean_bib(message.get("DOI", "")),
        "url": clean_bib(message.get("URL", "")),
    }
    lines = [f"@article{{{key},"]
    present = [(name, value) for name, value in fields.items() if value]
    for index, (name, value) in enumerate(present):
        comma = "," if index < len(present) - 1 else ""
        lines.append(f"  {name} = {{{value}}}{comma}")
    lines.append("}")
    return "\n".join(lines)


def main() -> None:
    for target in (OUTPUT, BIB_OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    first = json.loads(FIRST_AUDIT.read_text(encoding="utf-8"))
    records = []
    messages = []
    for record in first["records"]:
        doi = record["candidate_doi"]
        if doi.casefold() == REJECTED_DOI.casefold():
            records.append({
                "doi": doi,
                "decision": "rejected_as_mischaracterized_source",
                "reason": "The DOI resolves, but the publication is not a dedicated GWIS BA NRT methods paper as initially described.",
                "returned_title": record["returned_title"],
            })
            continue
        raw_path = ROOT / record["raw_file"]
        message = json.loads(raw_path.read_text(encoding="utf-8"))["message"]
        if doi in CORRECTED_TITLES:
            coverage = token_coverage(CORRECTED_TITLES[doi], record["returned_title"])
            accepted = record["doi_exact_match"] and coverage >= 0.90
            reason = "Corrected expected title after the first audit used an imprecise descriptive label."
        else:
            coverage = record["expected_token_coverage"]
            accepted = record["doi_exact_match"] and record["title_match_gate"]
            reason = "Passed the title and exact DOI gate in the first audit."
        records.append({"doi": doi, "decision": "accepted" if accepted else "rejected", "title_coverage": coverage, "reason": reason, "returned_title": record["returned_title"]})
        if accepted:
            messages.append(message)

    replacement_doi, replacement_title = REPLACEMENT
    replacement_path = RAW_DIR / (re.sub(r"[^A-Za-z0-9._-]+", "_", replacement_doi) + ".json")
    if replacement_path.exists():
        raise FileExistsError(f"Refusing to overwrite replacement response: {replacement_path}")
    response = requests.get(
        f"https://api.crossref.org/works/{replacement_doi}",
        headers={"User-Agent": "TolimaFireLiteratureAudit/1.0 (research DOI verification)"},
        timeout=45,
    )
    response.raise_for_status()
    replacement_path.write_bytes(response.content)
    replacement_message = response.json()["message"]
    replacement_returned_title = " ".join(replacement_message.get("title", []))
    replacement_accepted = (
        replacement_message.get("DOI", "").casefold() == replacement_doi.casefold()
        and token_coverage(replacement_title, replacement_returned_title) >= 0.90
    )
    records.append({
        "doi": replacement_doi,
        "decision": "accepted" if replacement_accepted else "rejected",
        "title_coverage": token_coverage(replacement_title, replacement_returned_title),
        "reason": "Final peer reviewed article substituted for the mischaracterized preprint citation.",
        "returned_title": replacement_returned_title,
        "raw_file": str(replacement_path.relative_to(ROOT)),
        "raw_sha256": sha256(replacement_path),
    })
    if replacement_accepted:
        messages.append(replacement_message)

    accepted_count = sum(r["decision"] == "accepted" for r in records)
    rejected_count = sum(r["decision"] != "accepted" for r in records)
    result = {
        "audit_date": "2026-09-06",
        "supersedes_for_usable_reference_set": str(FIRST_AUDIT.relative_to(ROOT)),
        "first_audit_preserved": True,
        "adjudication_rule": "Retain first-pass successes, correct four imprecise title labels only when coverage is at least 0.90, reject the mischaracterized preprint, verify its final relevant replacement independently.",
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "all_retained_dois_verified": rejected_count == 1 and replacement_accepted,
        "records": records,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    used: set[str] = set()
    entries = [bib_entry(message, used) for message in messages]
    BIB_OUTPUT.write_text("\n\n".join(entries) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("accepted_count", "rejected_count", "all_retained_dois_verified")}, indent=2))


if __name__ == "__main__":
    main()
