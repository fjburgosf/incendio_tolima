"""Build BibTeX only from recent Crossref records accepted as research articles."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "results" / "exploratory" / "verify_recent_literature_2026-09-09.json"
OUTPUT = ROOT / "results" / "exploratory" / "recent_research_articles_verified_2026-09-09.bib"


def clean(value: str) -> str:
    return html.unescape(str(value)).replace("{", "").replace("}", "").replace("\n", " ").strip()


def key_for(record: dict) -> str:
    authors = record["authors"]
    surname = re.sub(r"[^A-Za-z0-9]", "", clean(authors[0].get("family", "Research")))
    title_word = re.sub(r"[^A-Za-z0-9]", "", clean(record["title"]).split()[0])
    return f"{surname}{record['year']}{title_word}"


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite {OUTPUT}")
    payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    records = payload["records"]
    failures = [
        record for record in records
        if not record["accept"] or record["type"] != "journal-article"
    ]
    if failures:
        raise RuntimeError("Verification set contains a nonaccepted or nonarticle record")
    entries = []
    keys = set()
    for record in records:
        key = key_for(record)
        if key in keys:
            raise RuntimeError(f"Duplicate generated key {key}")
        keys.add(key)
        authors = " and ".join(
            ", ".join(part for part in (clean(author.get("family", "")), clean(author.get("given", ""))) if part)
            for author in record["authors"]
        )
        fields = [
            ("author", authors),
            ("title", clean(record["title"])),
            ("journal", clean(record["container"])),
            ("year", str(record["year"])),
            ("volume", clean(record.get("volume", ""))),
            ("number", clean(record.get("issue", ""))),
            ("pages", clean(record.get("page", "") or record.get("article_number", ""))),
            ("doi", clean(record["doi_requested"])),
            ("url", clean(record["url"])),
        ]
        retained = [f"  {name} = {{{value}}}" for name, value in fields if value]
        entries.append("@article{" + key + ",\n" + ",\n".join(retained) + "\n}\n")
    OUTPUT.write_text("\n".join(entries), encoding="utf-8")
    print(json.dumps({"article_count": len(entries), "keys": sorted(keys)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
