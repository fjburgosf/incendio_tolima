"""Download auditable regional ERA5 Land antecedent weather sources.

This script only retrieves raw source files. It does not construct accumulated
precipitation, calculate anomalies, average by department, or fit a model.
Those operations need a later audit because ERA5 Land precipitation messages
are interval accumulations rather than directly summable hourly values.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import cdsapi


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "configs" / "regional_preseason_prediction_plan_v1.json"
ENV_PATH = ROOT / ".env"
RAW_DIR = ROOT / "data" / "raw" / "era5_land" / "regional_preseason_2011_2026"
MANIFEST = RAW_DIR / "manifest_v1.json"
DATASET = "reanalysis-era5-land"
VARIABLES = ["total_precipitation", "volumetric_soil_water_layer_1"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    required = {"CDS_API_URL", "CDS_API_KEY"}
    missing = sorted(required - set(values))
    if missing:
        raise RuntimeError(f"Missing CDS credentials: {missing}")
    return values


def years_for_block(first: int, last: int) -> list[str]:
    return [str(year) for year in range(first, last + 1)]


def request(first: int, last: int, plan: dict[str, object]) -> dict[str, object]:
    north, west, south, east = plan["geography"]["screening_bbox_north_west_south_east"]
    return {
        "variable": VARIABLES,
        "year": years_for_block(first, last),
        "month": ["05", "06", "07"],
        "day": [f"{day:02d}" for day in range(1, 32)],
        "time": [f"{hour:02d}:00" for hour in range(24)],
        "data_format": "grib",
        "download_format": "unarchived",
        "area": [north, west, south, east],
    }


def main() -> None:
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    expected_seasons = list(plan["temporal_design"]["seasons"])
    if expected_seasons != list(range(2012, 2027)):
        raise RuntimeError("The frozen plan must contain uninterrupted 2012 through 2026 seasons")
    if MANIFEST.exists():
        raise FileExistsError(f"Manifest exists and freezes this retrieval: {MANIFEST}")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    credentials = read_env(ENV_PATH)
    client = cdsapi.Client(url=credentials["CDS_API_URL"], key=credentials["CDS_API_KEY"], quiet=True, progress=False)

    records = []
    blocks = [(2012, 2014), (2015, 2017), (2018, 2020), (2021, 2023), (2024, 2026)]
    for first, last in blocks:
        output = RAW_DIR / f"era5_land_regional_preseason_{first}_{last}_v1.grib"
        partial = output.with_suffix(output.suffix + ".partial")
        payload = request(first, last, plan)
        if partial.exists():
            raise FileExistsError(f"Incomplete partial retrieval needs inspection before reuse: {partial}")
        if output.exists():
            retrieval_status = "cached"
        else:
            client.retrieve(DATASET, payload).download(str(partial))
            partial.replace(output)
            retrieval_status = "retrieved"
        record = {
            "calendar_years": years_for_block(first, last),
            "months": ["05", "06", "07"],
            "file": str(output.relative_to(ROOT)).replace("\\", "/"),
            "bytes": output.stat().st_size,
            "sha256": sha256(output),
            "request": payload,
            "retrieval_status": retrieval_status,
        }
        records.append(record)
        print(json.dumps({"first_year": first, "last_year": last, "status": retrieval_status, "bytes": record["bytes"]}, ensure_ascii=False), flush=True)

    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "script": Path(__file__).name,
        "dataset": DATASET,
        "plan": str(PLAN_PATH.relative_to(ROOT)).replace("\\", "/"),
        "variables": VARIABLES,
        "records": records,
        "credential_handling": "Credentials were read locally and never written to this manifest.",
        "scope_limit": "This manifest establishes raw retrieval only. A later audit must check dimensions, times, units, interval semantics and department linkage before any variable construction.",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(MANIFEST.relative_to(ROOT)), "file_count": len(records), "total_bytes": sum(item["bytes"] for item in records)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
