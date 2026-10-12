"""Audit a minimal regional ERA5 Land request before the full retrieval.

The request is intentionally one day and two variables. It verifies that the
regional bounding box and the source variables are accepted by the service. It
does not establish a usable antecedent weather series.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cdsapi
import eccodes


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "configs" / "regional_preseason_prediction_plan_v1.json"
ENV_PATH = ROOT / ".env"
RAW_PATH = ROOT / "data" / "raw" / "era5_land" / "regional_preseason_2011_2026" / "audit_2026-07-31_tp_swvl1.grib"
AUDIT_PATH = ROOT / "results" / "exploratory" / "audit_era5_land_regional_preseason_access_v1.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    if not values.get("CDS_API_URL") or not values.get("CDS_API_KEY"):
        raise RuntimeError("CDS credentials are unavailable")
    return values


def inspect(path: Path) -> list[dict[str, object]]:
    records = []
    with path.open("rb") as handle:
        while message := eccodes.codes_grib_new_from_file(handle):
            try:
                records.append(
                    {
                        "short_name": str(eccodes.codes_get(message, "shortName")),
                        "units": str(eccodes.codes_get(message, "units")),
                        "validity_date": str(eccodes.codes_get(message, "validityDate")),
                        "validity_time": int(eccodes.codes_get(message, "validityTime")),
                        "value_count": int(eccodes.codes_get(message, "numberOfValues")),
                        "step_type": str(eccodes.codes_get(message, "stepType")),
                        "step_range": str(eccodes.codes_get(message, "stepRange")),
                    }
                )
            finally:
                eccodes.codes_release(message)
    return records


def main() -> None:
    if RAW_PATH.exists():
        raise FileExistsError(f"The audit raw file already exists: {RAW_PATH}")
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    north, west, south, east = plan["geography"]["screening_bbox_north_west_south_east"]
    request = {
        "variable": ["total_precipitation", "volumetric_soil_water_layer_1"],
        "year": ["2026"],
        "month": ["07"],
        "day": ["31"],
        "time": [f"{hour:02d}:00" for hour in range(24)],
        "data_format": "grib",
        "download_format": "unarchived",
        "area": [north, west, south, east],
    }
    credentials = read_env(ENV_PATH)
    RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
    partial = RAW_PATH.with_suffix(RAW_PATH.suffix + ".partial")
    client = cdsapi.Client(url=credentials["CDS_API_URL"], key=credentials["CDS_API_KEY"], quiet=True, progress=False)
    client.retrieve("reanalysis-era5-land", request).download(str(partial))
    partial.replace(RAW_PATH)
    records = inspect(RAW_PATH)
    audit = {
        "script": Path(__file__).name,
        "request": request,
        "raw_file": str(RAW_PATH.relative_to(ROOT)).replace("\\", "/"),
        "sha256": sha256(RAW_PATH),
        "bytes": RAW_PATH.stat().st_size,
        "message_count": len(records),
        "records": records,
        "acceptance": {
            "expected_message_count": 48,
            "all_messages_have_expected_variables": all(row["short_name"] in {"tp", "swvl1"} for row in records),
            "all_messages_have_same_spatial_count": len({row["value_count"] for row in records}) == 1,
        },
        "limit": "A successful small request does not establish the completeness or temporal semantics of the full source series.",
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"messages": len(records), "bytes": audit["bytes"], "value_counts": sorted({row["value_count"] for row in records})}, ensure_ascii=False))


if __name__ == "__main__":
    main()
