"""Resume the one-year ERA5 Land August climatology retrieval without overwriting raw files."""

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
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    if "CDS_API_URL" not in result or "CDS_API_KEY" not in result:
        raise RuntimeError("The local CDS credential configuration is incomplete")
    return result


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
    if MANIFEST.exists():
        raise FileExistsError(f"Completed manifest already exists: {MANIFEST}")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    years = list(range(int(plan["years"]["first"]), int(plan["years"]["last"]) + 1))
    outputs = {year: RAW / f"era5_land_core_hourly_{year}_08_tolima_screening_bbox.grib" for year in years}
    partials = [path.with_suffix(path.suffix + ".partial") for path in outputs.values() if path.with_suffix(path.suffix + ".partial").exists()]
    if partials:
        raise RuntimeError(f"A partial file requires inspection before resume: {partials[0]}")
    credentials = read_env(ENV)
    client = cdsapi.Client(url=credentials["CDS_API_URL"], key=credentials["CDS_API_KEY"], quiet=True, progress=False)
    for year, output in outputs.items():
        if output.exists():
            continue
        partial = output.with_suffix(output.suffix + ".partial")
        client.retrieve("reanalysis-era5-land", request(year, plan)).download(str(partial))
        partial.replace(output)
        print(json.dumps({"retrieved_year": year, "bytes": output.stat().st_size}, ensure_ascii=False), flush=True)
    files = []
    for year, output in outputs.items():
        if not output.exists():
            raise RuntimeError(f"Missing expected climatology file after resume: {output}")
        files.append({"year": year, "file": str(output.relative_to(ROOT)).replace("\\", "/"), "bytes": output.stat().st_size, "sha256": sha256(output), "request": request(year, plan)})
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": "reanalysis-era5-land",
        "plan": str(PLAN.relative_to(ROOT)).replace("\\", "/"),
        "files": files,
        "credential_handling": "Credentials were read from the local environment file and are not written to this manifest.",
        "resumption": "Existing raw files were retained and hashed. Missing years were retrieved without overwriting completed years.",
        "interpretation_limit": "This manifest records source retrieval only. A separate audit must verify every file before atmospheric aggregation or climatological comparison.",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(MANIFEST.relative_to(ROOT)), "file_count": len(files), "total_bytes": sum(item["bytes"] for item in files)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
