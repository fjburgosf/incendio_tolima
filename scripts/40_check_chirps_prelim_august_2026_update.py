"""Revisa sin sobrescribir la disponibilidad de CHIRPS para agosto de 2026."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
INDEX_URL = "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/prelim/sat/2026/"
OUTPUT = ROOT / "results" / "exploratory" / "check_chirps_prelim_availability_august_2026_2026_09_01.json"
PATTERN = re.compile(r"chirps-v3\.0\.prelim\.2026\.08\.(\d{2})\.tif")


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("La comprobación actualizada de CHIRPS ya existe.")
    with urlopen(INDEX_URL, timeout=60) as response:
        index = response.read().decode("utf-8")
    available_days = sorted({int(day) for day in PATTERN.findall(index)})
    expected_days = set(range(1, 32))
    result = {
        "index_url": INDEX_URL,
        "product": "CHIRPS v3 preliminar satelital diario",
        "available_days": available_days,
        "missing_days": sorted(expected_days - set(available_days)),
        "first_available_day": min(available_days) if available_days else None,
        "last_available_day": max(available_days) if available_days else None,
        "interpretation_limit": "La presencia en el índice verifica archivo publicado, no calidad local, totales, anomalías o cobertura de incendio.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
