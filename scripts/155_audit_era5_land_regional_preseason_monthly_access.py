"""Audit one complete regional month before scheduled ERA5 Land retrieval.

The preceding three year request was rejected by the CDS size limit. This
script tests the smallest scientifically usable source block, a calendar month
with both antecedent predictor variables and all hours. It is an access audit,
not an analytical input.
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
RAW_PATH = ROOT / "data" / "raw" / "era5_land" / "regional_preseason_2011_2026" / "audit_2026-07_month_tp_swvl1.grib"
AUDIT_PATH = ROOT / "results" / "exploratory" / "audit_era5_land_regional_preseason_monthly_access_v1.json"


def read_env(path: Path) -> dict[str, str]:
    pairs = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            pairs[key.strip()] = value.strip()
    if not pairs.get("CDS_API_URL") or not pairs.get("CDS_API_KEY"):
        raise RuntimeError("CDS credentials are unavailable")
    return pairs


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def inspect(path: Path) -> dict[str, object]:
    messages = []
    with path.open("rb") as handle:
        while message := eccodes.codes_grib_new_from_file(handle):
            try:
                messages.append(
                    {
                        "short_name": str(eccodes.codes_get(message, "shortName")),
                        "units": str(eccodes.codes_get(message, "units")),
                        "validity_date": str(eccodes.codes_get(message, "validityDate")),
                        "validity_time": int(eccodes.codes_get(message, "validityTime")),
                        "value_count": int(eccodes.codes_get(message, "numberOfValues")),
                        "step_type": str(eccodes.codes_get(message, "stepType")),
                    }
                )
            finally:
                eccodes.codes_release(message)
    return {
        "message_count": len(messages),
        "variables": sorted({row["short_name"] for row in messages}),
        "units": sorted({row["units"] for row in messages}),
        "spatial_value_counts": sorted({row["value_count"] for row in messages}),
        "dates": sorted({row["validity_date"] for row in messages}),
        "step_types": sorted({row["step_type"] for row in messages}),
    }


def main() -> None:
    if RAW_PATH.exists() or RAW_PATH.with_suffix(RAW_PATH.suffix + ".partial").exists():
        raise FileExistsError(f"Monthly audit output already exists: {RAW_PATH}")
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    north, west, south, east = plan["geography"]["screening_bbox_north_west_south_east"]
    request = {
        "variable": ["total_precipitation", "volumetric_soil_water_layer_1"],
        "year": ["2026"],
        "month": ["07"],
        "day": [f"{day:02d}" for day in range(1, 32)],
        "time": [f"{hour:02d}:00" for hour in range(24)],
        "data_format": "grib",
        "download_format": "unarchived",
        "area": [north, west, south, east],
    }
    credentials = read_env(ENV_PATH)
    client = cdsapi.Client(url=credentials["CDS_API_URL"], key=credentials["CDS_API_KEY"], quiet=True, progress=False)
    RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
    partial = RAW_PATH.with_suffix(RAW_PATH.suffix + ".partial")
    client.retrieve("reanalysis-era5-land", request).download(str(partial))
    partial.replace(RAW_PATH)
    summary = inspect(RAW_PATH)
    audit = {
        "script": Path(__file__).name,
        "request": request,
        "raw_file": str(RAW_PATH.relative_to(ROOT)).replace("\\", "/"),
        "bytes": RAW_PATH.stat().st_size,
        "sha256": sha256(RAW_PATH),
        "summary": summary,
        "acceptance": {
            "expected_message_count": 1488,
            "message_count_matches": summary["message_count"] == 1488,
            "variables_match": summary["variables"] == ["swvl1", "tp"],
            "one_spatial_value_count": len(summary["spatial_value_counts"]) == 1,
        },
        "limit": "The test demonstrates whether a monthly block is technically accepted. It does not establish the complete regional source series.",
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"bytes": audit["bytes"], "summary": summary, "acceptance": audit["acceptance"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
