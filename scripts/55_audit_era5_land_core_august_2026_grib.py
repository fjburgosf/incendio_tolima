"""Audita estructura y continuidad de los cinco GRIB horarios de agosto de 2026."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import eccodes


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "era5_land"
OUTPUT = ROOT / "results" / "exploratory" / "audit_era5_land_core_august_2026_grib.json"
FILES = sorted(RAW_DIR.glob("era5_land_*_hourly_2026-08_tolima_screening_bbox.grib"))


def safe_get(message, key: str):
    try:
        return eccodes.codes_get(message, key)
    except eccodes.KeyValueNotFoundError:
        return None


def audit_file(path: Path) -> dict[str, object]:
    messages: list[dict[str, object]] = []
    with path.open("rb") as stream:
        while True:
            message = eccodes.codes_grib_new_from_file(stream)
            if message is None:
                break
            try:
                values = eccodes.codes_get_array(message, "values")
                messages.append(
                    {
                        "short_name": safe_get(message, "shortName"),
                        "name": safe_get(message, "name"),
                        "units": safe_get(message, "units"),
                        "validity_date": safe_get(message, "validityDate"),
                        "validity_time": safe_get(message, "validityTime"),
                        "step_range": safe_get(message, "stepRange"),
                        "number_of_points": safe_get(message, "numberOfPoints"),
                        "minimum": float(values.min()),
                        "maximum": float(values.max()),
                    }
                )
            finally:
                eccodes.codes_release(message)
    datetimes = [(item["validity_date"], item["validity_time"]) for item in messages]
    return {
        "file": str(path.relative_to(ROOT)).replace("\\", "/"),
        "message_count": len(messages),
        "expected_hourly_message_count": 31 * 24,
        "unique_validity_datetimes": len(set(datetimes)),
        "duplicate_validity_datetime_messages": len(datetimes) - len(set(datetimes)),
        "first_validity_date": min(item["validity_date"] for item in messages),
        "last_validity_date": max(item["validity_date"] for item in messages),
        "unique_validity_times": sorted(set(item["validity_time"] for item in messages)),
        "short_names": sorted(set(item["short_name"] for item in messages)),
        "names": sorted(set(item["name"] for item in messages)),
        "units": sorted(set(item["units"] for item in messages)),
        "step_ranges": sorted(set(str(item["step_range"]) for item in messages)),
        "number_of_points_counts": dict(Counter(str(item["number_of_points"]) for item in messages)),
        "global_minimum": min(item["minimum"] for item in messages),
        "global_maximum": max(item["maximum"] for item in messages),
    }


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribirá")
    if len(FILES) != 5:
        raise ValueError(f"Se esperaban cinco archivos y se encontraron {len(FILES)}")
    result = {
        "dataset": "reanalysis-era5-land",
        "period": "2026-08-01 through 2026-08-31",
        "files": [audit_file(path) for path in FILES],
        "interpretation_limit": "La auditoría verifica estructura horaria, metadatos y rangos. No crea variables derivadas, agregaciones municipales, asociaciones ni resultados del estudio.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
