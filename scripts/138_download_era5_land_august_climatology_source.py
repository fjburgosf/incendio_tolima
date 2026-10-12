"""Download raw one-year August ERA5 Land climatology files without deriving variables."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import cdsapi


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "era5_land_august_climatology_source_plan_v1.json"
ENV = ROOT / ".env"
RAW = ROOT / "data" / "raw" / "era5_land" / "august_climatology_1991_2025"
MANIFEST = RAW / "era5_land_august_climatology_source_2026-09-08_manifest_v1.json"


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
    missing = required - set(values)
    if missing:
        raise RuntimeError(f"CDS credentials are missing from the local environment file: {sorted(missing)}")
    return values


def request(year: int, plan: dict[str, object]) -> dict[str, object]:
    return {
        "variable": plan["variables"],
        "year": [str(year)],
        "month": [plan["month"]],
        "day": [f"{day:02d}" for day in range(1, 32)],
        "time": [f"{hour:02d}:00" for hour in range(24)],
        "data_format": "grib",
        "download_format": "unarchived",
        "area": plan["spatial_request"]["bbox_north_west_south_east"],
    }


def main() -> None:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    years = list(range(int(plan["years"]["first"]), int(plan["years"]["last"]) + 1))
    RAW.mkdir(parents=True, exist_ok=True)
    if MANIFEST.exists():
        raise FileExistsError(f"A climatology manifest already exists: {MANIFEST}")
    outputs = {year: RAW / f"era5_land_core_hourly_{year}_08_tolima_screening_bbox.grib" for year in years}
    existing = [path for path in outputs.values() if path.exists() or path.with_suffix(path.suffix + ".partial").exists()]
    if existing:
        raise FileExistsError(f"Climatology source directory needs inspection before retrieval, first existing file: {existing[0]}")
    credentials = read_env(ENV)
    client = cdsapi.Client(url=credentials["CDS_API_URL"], key=credentials["CDS_API_KEY"], quiet=True, progress=False)
    records = []
    for year, output in outputs.items():
        partial = output.with_suffix(output.suffix + ".partial")
        client.retrieve("reanalysis-era5-land", request(year, plan)).download(str(partial))
        partial.replace(output)
        records.append({"year": year, "file": str(output.relative_to(ROOT)).replace("\\", "/"), "bytes": output.stat().st_size, "sha256": sha256(output), "request": request(year, plan)})
        print(json.dumps({"retrieved_year": year, "bytes": output.stat().st_size}, ensure_ascii=False), flush=True)
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": "reanalysis-era5-land",
        "plan": str(PLAN.relative_to(ROOT)).replace("\\", "/"),
        "files": records,
        "credential_handling": "Credentials were read from the local environment file and are not written to this manifest.",
        "interpretation_limit": "This manifest records source retrieval only. A separate audit must verify every file before atmospheric aggregation or historical comparison.",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(MANIFEST.relative_to(ROOT)), "file_count": len(records), "total_bytes": sum(record["bytes"] for record in records)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
