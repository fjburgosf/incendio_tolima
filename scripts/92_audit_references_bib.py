"""Audit references.bib against the frozen Crossref evidence."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BIB = ROOT / "references.bib"
ADJUDICATION = ROOT / "results" / "exploratory" / "adjudicate_literature_dois_2026-09-06.json"
FIRST_AUDIT = ROOT / "results" / "exploratory" / "verify_literature_dois_2026-09-06.json"
OUTPUT = ROOT / "results" / "exploratory" / "audit_references_bib_2026-09-06.json"


def normalized(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value or "")
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", value).strip()


def year(message: dict) -> str:
    for field in ("published-print", "published-online", "published", "issued"):
        parts = message.get(field, {}).get("date-parts", [])
        if parts and parts[0]:
            return str(parts[0][0])
    return ""


def parse_bib(text: str) -> list[dict]:
    blocks = re.findall(r"@article\{([^,]+),\s*(.*?)\n\}", text, flags=re.S | re.I)
    entries = []
    for key, body in blocks:
        fields = {name.casefold(): value for name, value in re.findall(r"^\s*([A-Za-z]+)\s*=\s*\{(.*?)\},?\s*$", body, flags=re.M)}
        entries.append({"key": key, "fields": fields})
    return entries


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {OUTPUT}")
    adjudication = json.loads(ADJUDICATION.read_text(encoding="utf-8"))
    first = json.loads(FIRST_AUDIT.read_text(encoding="utf-8"))
    raw_by_doi = {r["candidate_doi"].casefold(): ROOT / r["raw_file"] for r in first["records"]}
    replacement = next(r for r in adjudication["records"] if r["doi"].casefold() == "10.5194/bg-22-5247-2025")
    raw_by_doi[replacement["doi"].casefold()] = ROOT / replacement["raw_file"]
    accepted = {r["doi"].casefold() for r in adjudication["records"] if r["decision"] == "accepted"}
    rejected = {r["doi"].casefold() for r in adjudication["records"] if r["decision"] != "accepted"}

    entries = parse_bib(BIB.read_text(encoding="utf-8"))
    doi_entries: dict[str, list[dict]] = {}
    for entry in entries:
        doi = entry["fields"].get("doi", "").casefold()
        doi_entries.setdefault(doi, []).append(entry)

    checks = []
    for doi in sorted(accepted):
        matches = doi_entries.get(doi, [])
        item = {"doi": doi, "entry_count": len(matches), "checks": {}}
        if len(matches) == 1:
            entry = matches[0]
            message = json.loads(raw_by_doi[doi].read_text(encoding="utf-8"))["message"]
            fields = entry["fields"]
            authors = message.get("author", [])
            first_family = authors[0].get("family", "") if authors else ""
            item["citation_key"] = entry["key"]
            item["checks"] = {
                "title_exact": normalized(fields.get("title", "")) == normalized(" ".join(message.get("title", []))),
                "journal_exact": normalized(fields.get("journal", "")) == normalized(" ".join(message.get("container-title", []))),
                "year_exact": fields.get("year", "") == year(message),
                "first_author_present": normalized(first_family) in normalized(fields.get("author", "")),
                "doi_url_exact": normalized(fields.get("url", "")) == normalized(message.get("URL", "")),
            }
        checks.append(item)

    missing = sorted(accepted - set(doi_entries))
    extras = sorted(set(doi_entries) - accepted - {""})
    duplicate_dois = sorted(doi for doi, values in doi_entries.items() if doi and len(values) != 1)
    duplicate_keys = sorted({e["key"] for e in entries if sum(x["key"] == e["key"] for x in entries) > 1})
    field_failures = [
        {"doi": item["doi"], "failed": [name for name, passed in item["checks"].items() if not passed]}
        for item in checks
        if item["entry_count"] != 1 or not all(item["checks"].values())
    ]
    rejected_present = sorted(rejected & set(doi_entries))
    passed = not any((missing, extras, duplicate_dois, duplicate_keys, field_failures, rejected_present)) and len(entries) == len(accepted)
    result = {
        "audit_date": "2026-09-06",
        "bib_file": str(BIB.relative_to(ROOT)),
        "accepted_doi_count": len(accepted),
        "bib_entry_count": len(entries),
        "missing_accepted_dois": missing,
        "unexpected_dois": extras,
        "duplicate_dois": duplicate_dois,
        "duplicate_citation_keys": duplicate_keys,
        "rejected_dois_present": rejected_present,
        "field_failures": field_failures,
        "all_checks_passed": passed,
        "records": checks,
        "scope_note": "This audit verifies bibliography identity and completeness. Numeric order and citation matching require a manuscript and are not yet testable.",
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("accepted_doi_count", "bib_entry_count", "field_failures", "all_checks_passed")}, ensure_ascii=False, indent=2))
    if not passed:
        raise RuntimeError("references.bib audit failed")


if __name__ == "__main__":
    main()
