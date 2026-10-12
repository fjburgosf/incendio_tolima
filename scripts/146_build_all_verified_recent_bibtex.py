"""Build one BibTeX fragment from both verified recent research article audits."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    ROOT / "results" / "exploratory" / "verify_recent_literature_2026-09-09.json",
    ROOT / "results" / "exploratory" / "verify_additional_recent_research_2026-09-09.json",
]
OUTPUT = ROOT / "results" / "exploratory" / "all_new_verified_recent_research_2026-09-09.bib"


def clean(value: str) -> str:
    return html.unescape(str(value)).replace("{", "").replace("}", "").replace("\n", " ").strip()


def build_key(record: dict) -> str:
    surname = re.sub(r"[^A-Za-z0-9]", "", clean(record["authors"][0].get("family", "Research")))
    word = re.sub(r"[^A-Za-z0-9]", "", clean(record["title"]).split()[0])
    return f"{surname}{record['year']}{word}"


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite {OUTPUT}")
    records = []
    for source in SOURCES:
        records.extend(json.loads(source.read_text(encoding="utf-8"))["records"])
    if any(not r["accept"] or r["type"] != "journal-article" for r in records):
        raise RuntimeError("Only accepted research articles can enter the bibliography fragment")
    keys = set()
    entries = []
    for record in records:
        key = build_key(record)
        if key in keys:
            raise RuntimeError(f"Duplicate key {key}")
        keys.add(key)
        author = " and ".join(
            ", ".join(part for part in (clean(a.get("family", "")), clean(a.get("given", ""))) if part)
            for a in record["authors"]
        )
        fields = [
            ("author", author),
            ("title", clean(record["title"])),
            ("journal", clean(record["container"])),
            ("year", str(record["year"])),
            ("volume", clean(record.get("volume", ""))),
            ("number", clean(record.get("issue", ""))),
            ("pages", clean(record.get("page", "") or record.get("article_number", ""))),
            ("doi", clean(record["doi_requested"])),
            ("url", clean(record["url"])),
        ]
        body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields if value)
        entries.append(f"@article{{{key},\n{body}\n}}\n")
    OUTPUT.write_text("\n".join(entries), encoding="utf-8")
    print(json.dumps({"article_count": len(entries), "keys": sorted(keys)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
