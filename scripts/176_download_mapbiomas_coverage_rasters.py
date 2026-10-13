"""Descarga los mapas anuales de cobertura MapBiomas Colombia colección 3 de 2011 a 2024.

Sigue `configs/forest_cover_detection_plan_v1.json`. Cada archivo se conserva tal
como se publica y se acepta solo si su md5 coincide con el que declara el
almacenamiento público. Los archivos ya presentes y válidos no se vuelven a bajar.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.request
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "raw" / "mapbiomas" / "coverage_collection3"
MANIFEST = OUT / "manifest_v1.json"
BUCKET = "mapbiomas-public"
PREFIX = "initiatives/colombia/collection_3/coverage/colombia_coverage_{year}.tif"
YEARS = range(2011, 2025)
HEADERS = {"User-Agent": "reproducible-audit/1.0"}


def metadata(name: str) -> dict:
    url = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/{urllib.request.quote(name, safe='')}?fields=name,size,md5Hash,updated"
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def md5_base64(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return base64.b64encode(digest.digest()).decode("ascii")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    entries = []
    for year in YEARS:
        name = PREFIX.format(year=year)
        meta = metadata(name)
        path = OUT / Path(name).name
        if not (path.exists() and md5_base64(path) == meta["md5Hash"]):
            partial = path.with_suffix(".tif.partial")
            url = f"https://storage.googleapis.com/{BUCKET}/{name}"
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=600) as response, partial.open("wb") as handle:
                while block := response.read(1024 * 1024):
                    handle.write(block)
            if md5_base64(partial) != meta["md5Hash"]:
                partial.unlink()
                raise RuntimeError(f"md5 distinto en {name}")
            partial.replace(path)
        entries.append({"year": year, "url": f"https://storage.googleapis.com/{BUCKET}/{name}", "path": str(path.relative_to(ROOT)).replace("\\", "/"),
                        "size": int(meta["size"]), "md5_base64": meta["md5Hash"], "source_updated": meta["updated"]})
        print(year, "ok", flush=True)
    MANIFEST.write_text(json.dumps({"script": Path(__file__).name, "retrieved": date.today().isoformat(), "files": entries}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
