"""Identifica las horas faltantes en los GRIB ERA5 Land de agosto de 2026."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import eccodes


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "era5_land"
OUTPUT = ROOT / "results" / "exploratory" / "identify_missing_era5_land_august_hours.json"


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("El control ya existe y no se sobrescribirá")
    expected = {
        datetime(2026, 8, 1) + timedelta(hours=hour)
        for hour in range(31 * 24)
    }
    records = []
    for path in sorted(RAW_DIR.glob("era5_land_*_hourly_2026-08_tolima_screening_bbox.grib")):
        observed = set()
        short_names = set()
        with path.open("rb") as stream:
            while True:
                message = eccodes.codes_grib_new_from_file(stream)
                if message is None:
                    break
                try:
                    date = str(eccodes.codes_get(message, "validityDate"))
                    time = int(eccodes.codes_get(message, "validityTime"))
                    observed.add(datetime.strptime(f"{date}{time:04d}", "%Y%m%d%H%M"))
                    short_names.add(eccodes.codes_get(message, "shortName"))
                finally:
                    eccodes.codes_release(message)
        records.append(
            {
                "file": str(path.relative_to(ROOT)).replace("\\", "/"),
                "short_names": sorted(short_names),
                "observed_count": len(observed),
                "missing_utc": [value.strftime("%Y-%m-%dT%H:%MZ") for value in sorted(expected - observed)],
                "unexpected_utc": [value.strftime("%Y-%m-%dT%H:%MZ") for value in sorted(observed - expected)],
            }
        )
    result = {
        "expected_utc_count": len(expected),
        "files": records,
        "interpretation_limit": "Identifica continuidad temporal por archivo. No imputa horas ni construye variables derivadas.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
