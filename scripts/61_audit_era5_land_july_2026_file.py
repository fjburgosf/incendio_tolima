"""Audita continuidad y estructura del archivo ERA5 Land de julio de 2026."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import eccodes


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "raw" / "era5_land" / "era5_land_core_hourly_2026-07_tolima_screening_bbox.grib"
OUTPUT = ROOT / "results" / "exploratory" / "audit_era5_land_july_2026_file.json"


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribirá")
    expected = {datetime(2026, 7, 1) + timedelta(hours=hour) for hour in range(31 * 24)}
    observed: dict[str, set[datetime]] = defaultdict(set)
    metadata: dict[str, dict[str, object]] = {}
    message_count = 0
    with SOURCE.open("rb") as stream:
        while True:
            message = eccodes.codes_grib_new_from_file(stream)
            if message is None:
                break
            try:
                short_name = eccodes.codes_get(message, "shortName")
                date = str(eccodes.codes_get(message, "validityDate"))
                time = int(eccodes.codes_get(message, "validityTime"))
                observed[short_name].add(datetime.strptime(f"{date}{time:04d}", "%Y%m%d%H%M"))
                metadata[short_name] = {
                    "name": eccodes.codes_get(message, "name"),
                    "units": eccodes.codes_get(message, "units"),
                    "number_of_points": eccodes.codes_get(message, "numberOfPoints"),
                }
                message_count += 1
            finally:
                eccodes.codes_release(message)
    variables = {
        short_name: {
            **metadata[short_name],
            "observed_hour_count": len(datetimes),
            "missing_utc": [value.strftime("%Y-%m-%dT%H:%MZ") for value in sorted(expected - datetimes)],
            "unexpected_utc": [value.strftime("%Y-%m-%dT%H:%MZ") for value in sorted(datetimes - expected)],
        }
        for short_name, datetimes in sorted(observed.items())
    }
    hasher = hashlib.sha256(SOURCE.read_bytes()).hexdigest().upper()
    result = {
        "source_file": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
        "bytes": SOURCE.stat().st_size,
        "sha256": hasher,
        "message_count": message_count,
        "expected_message_count": 31 * 24 * 5,
        "variables": variables,
        "interpretation_limit": "Verifica continuidad y esquema del archivo crudo. No crea variables derivadas, agregaciones ni resultados analíticos.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
