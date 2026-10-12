"""Decodifica metadatos de las variables candidatas de ERA5 Land."""

from __future__ import annotations

import json
from pathlib import Path

import eccodes


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "raw" / "era5_land" / "era5_land_audit_candidate_variables_2026-08-20T00.grib"
OUTPUT = ROOT / "results" / "exploratory" / "inspect_era5_land_candidate_variables.json"


def get_value(message, key: str):
    try:
        return eccodes.codes_get(message, key)
    except eccodes.KeyValueNotFoundError:
        return None


def main() -> None:
    messages = []
    with SOURCE.open("rb") as stream:
        while True:
            message = eccodes.codes_grib_new_from_file(stream)
            if message is None:
                break
            try:
                values = eccodes.codes_get_array(message, "values")
                messages.append(
                    {
                        "short_name": get_value(message, "shortName"),
                        "parameter_name": get_value(message, "name"),
                        "units": get_value(message, "units"),
                        "validity_date": get_value(message, "validityDate"),
                        "validity_time": get_value(message, "validityTime"),
                        "data_date": get_value(message, "dataDate"),
                        "step_range": get_value(message, "stepRange"),
                        "number_of_points": get_value(message, "numberOfPoints"),
                        "minimum_value": float(values.min()),
                        "maximum_value": float(values.max()),
                    }
                )
            finally:
                eccodes.codes_release(message)
    audit = {
        "source_file": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
        "message_count": len(messages),
        "messages": messages,
        "interpretation_limit": (
            "La inspección verifica disponibilidad y metadatos de una sola hora. No estima "
            "promedios, acumulados diarios, anomalías, relaciones predictivas o causalidad."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
