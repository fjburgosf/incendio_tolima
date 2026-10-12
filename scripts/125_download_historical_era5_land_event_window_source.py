"""Download pre-specified historical ERA5-Land source coverage without analysis."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import cdsapi


ROOT = Path(__file__).resolve().parents[1]
ACCESS_DATE = "2026-09-07"
ENV_FILE = ROOT / ".env"
PLAN = ROOT / "configs" / "historical_era5_land_event_window_source_plan_v1.json"
RAW_DIR = ROOT / "data" / "raw" / "era5_land"
MANIFEST = RAW_DIR / f"era5_land_historical_event_window_source_{ACCESS_DATE}_manifest_v2.json"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def request_for(year: int, period: str, plan: dict[str, object]) -> dict[str, object]:
    base = {
        "variable": plan["variables"],
        "year": [str(year)],
        "time": [f"{hour:02d}:00" for hour in range(24)],
        "data_format": "grib",
        "download_format": "unarchived",
        "area": plan["spatial_request"]["bbox_north_west_south_east"],
    }
    if period == "august":
        return {**base, "month": ["08"], "day": [f"{day:02d}" for day in range(1, 32)]}
    if period == "september_extension":
        return {**base, "month": ["09"], "day": ["01"]}
    raise ValueError(f"Unknown period: {period}")


def main() -> None:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    outputs = {
        (year, period): RAW_DIR / f"era5_land_historical_core_hourly_{year}_{'08' if period == 'august' else '09_01'}_tolima_screening_bbox.grib"
        for year in plan["years"]
        for period in ("august", "september_extension")
    }
    for path in [*outputs.values(), MANIFEST]:
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {path}")
        partial = path.with_suffix(path.suffix + ".partial")
        if partial.exists():
            raise FileExistsError(f"Partial retrieval needs inspection before retry: {partial}")
    credentials = read_env(ENV_FILE)
    client = cdsapi.Client(
        url=credentials["CDS_API_URL"], key=credentials["CDS_API_KEY"], quiet=True, progress=False
    )
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for (year, period), output in outputs.items():
        request = request_for(year, period, plan)
        partial = output.with_suffix(output.suffix + ".partial")
        client.retrieve("reanalysis-era5-land", request).download(str(partial))
        partial.replace(output)
        files.append(
            {
                "year": year,
                "period": period,
                "file": str(output.relative_to(ROOT)).replace("\\", "/"),
                "bytes": output.stat().st_size,
                "sha256": sha256(output),
                "request": request,
            }
        )
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": "reanalysis-era5-land",
        "plan": str(PLAN.relative_to(ROOT)).replace("\\", "/"),
        "files": files,
        "credential_handling": "Credentials are read from .env and are not written in this manifest.",
        "interpretation_limit": "This manifest records source retrieval only. Structure, continuity, grid consistency and units require a separate audit before any event feature construction.",
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(MANIFEST), "files": [{"year": item["year"], "period": item["period"], "bytes": item["bytes"]} for item in files]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
