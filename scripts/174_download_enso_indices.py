"""Descarga y audita los índices ENSO de NOAA CPC para la enmienda del modelo regional.

Sigue `configs/regional_preseason_prediction_enso_amendment_v1.json`. Conserva sin
cambios la tabla ONI en texto y la página HTML de RONI, y extrae las estaciones
trimestrales AMJ, MJJ y JJA de 2012 a 2026. JJA se guarda solo como descriptor,
porque incluye agosto y no puede entrar en un modelo de anticipación.
"""

from __future__ import annotations

import csv
import hashlib
import json
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "enso"
ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
RONI_URL = "https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso/roni/"
OUTPUT = ROOT / "data" / "interim" / "enso_indices_2012_2026_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_enso_indices_v1.json"
SEASONS = range(2012, 2027)
HEADER = ["DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ"]


class TableRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "reproducible-audit/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def main() -> None:
    stamp = date.today().isoformat()
    RAW.mkdir(parents=True, exist_ok=True)
    oni_path = RAW / f"oni_ascii_{stamp}.txt"
    roni_path = RAW / f"roni_page_{stamp}.html"
    for path, url in ((oni_path, ONI_URL), (roni_path, RONI_URL)):
        if not path.exists():
            path.write_bytes(fetch(url))

    oni: dict[tuple[str, int], float] = {}
    for line in oni_path.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split()
        if len(parts) == 4:
            oni[(parts[0], int(parts[1]))] = float(parts[3])

    parser = TableRows()
    parser.feed(roni_path.read_text(encoding="utf-8", errors="replace"))
    roni: dict[tuple[str, int], float] = {}
    for row in parser.rows:
        if len(row) >= 2 and row[0].isdigit() and len(row[0]) == 4:
            for label, value in zip(HEADER, row[1:]):
                if value:
                    roni[(label, int(row[0]))] = float(value)

    rows = []
    for season in SEASONS:
        record = {"season": season}
        for label in ("AMJ", "MJJ", "JJA"):
            if (label, season) not in oni:
                raise RuntimeError(f"Falta ONI {label} {season}")
            record[f"oni_{label.lower()}"] = oni[(label, season)]
        if ("MJJ", season) not in roni:
            raise RuntimeError(f"Falta RONI MJJ {season}")
        record["roni_mjj"] = roni[("MJJ", season)]
        rows.append(record)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    reference = [r for r in rows if r["season"] != 2026]
    target = [r for r in rows if r["season"] == 2026][0]
    audit = {
        "script": Path(__file__).name,
        "retrieved": stamp,
        "sources": [
            {"url": ONI_URL, "path": str(oni_path.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(oni_path.read_bytes())},
            {"url": RONI_URL, "path": str(roni_path.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(roni_path.read_bytes())},
        ],
        "roni_years_parsed": sorted({year for _, year in roni}),
        "values_2026": target,
        "reference_maximum_2012_2025": {k: max(r[k] for r in reference) for k in target if k != "season"},
        "extrapolation_2026": {k: target[k] > max(r[k] for r in reference) for k in target if k != "season"},
        "output": str(OUTPUT.relative_to(ROOT)).replace("\\", "/"),
        "notes": [
            "Valores vigentes en la fecha de descarga. NOAA revisa los valores recientes hasta dos meses después.",
            "JJA incluye agosto y solo se usa como descriptor.",
        ],
    }
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
