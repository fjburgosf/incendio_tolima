"""Audita el archivo de junio producido antes de cancelar la solicitud CDS grande."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import eccodes


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "raw" / "era5_land" / "era5_land_core_hourly_2026-06_tolima_screening_bbox.grib"
OUTPUT = ROOT / "results" / "exploratory" / "audit_recovered_era5_land_june_2026_file.json"


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest().upper()


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribirá")
    expected = {datetime(2026, 6, 1) + timedelta(hours=hour) for hour in range(30 * 24)}
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
    variables = {}
    for short_name, datetimes in sorted(observed.items()):
        variables[short_name] = {
            **metadata[short_name],
            "observed_hour_count": len(datetimes),
            "missing_utc": [value.strftime("%Y-%m-%dT%H:%MZ") for value in sorted(expected - datetimes)],
            "unexpected_utc": [value.strftime("%Y-%m-%dT%H:%MZ") for value in sorted(datetimes - expected)],
        }
    result = {
        "source_file": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
        "bytes": SOURCE.stat().st_size,
        "sha256": sha256(SOURCE),
        "message_count": message_count,
        "expected_message_count": 30 * 24 * 5,
        "variables": variables,
        "interpretation_limit": "El archivo fue producido por una solicitud documentada en el script 53, pero el proceso se canceló antes de escribir su manifiesto. Esta auditoría verifica su contenido sin modificarlo. No crea resultados analíticos.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
