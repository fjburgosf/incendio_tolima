"""Download the CHIRPS v3 monthly source series for regional prediction.

The source grid is global. Each file is preserved exactly as published and is
only later reduced to departmental summaries through a documented pixel centre
rule. Downloading is serial and resumable so the public server is not flooded.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "configs" / "regional_preseason_prediction_plan_v1.json"
RAW_DIR = ROOT / "data" / "raw" / "chirps_v3" / "monthly_global_cogs_2012_2026"
AUDIT_RAW = ROOT / "data" / "raw" / "chirps_v3" / "chirps-v3.0.2026.07.monthly.global.cog"
MANIFEST = RAW_DIR / "manifest_v1.json"
BASE_URL = "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/monthly/global/cogs"
MONTHS = (5, 6, 7)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def date_grid() -> list[tuple[int, int]]:
    return [(year, month) for year in range(2012, 2027) for month in MONTHS]


def source_url(year: int, month: int) -> str:
    return f"{BASE_URL}/chirps-v3.0.{year}.{month:02d}.cog"


def output_path(year: int, month: int) -> Path:
    return RAW_DIR / f"chirps-v3.0.{year}.{month:02d}.monthly.global.cog"


def retrieve(url: str, output: Path) -> None:
    partial = output.with_suffix(output.suffix + ".partial")
    if partial.exists():
        raise FileExistsError(f"Incomplete CHIRPS retrieval needs inspection: {partial}")
    with urlopen(url, timeout=180) as response, partial.open("wb") as handle:
        while block := response.read(1024 * 1024):
            handle.write(block)
    partial.replace(output)


def main() -> None:
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    if MANIFEST.exists():
        raise FileExistsError(f"Manifest exists and freezes this source retrieval: {MANIFEST}")
    expected = [(year, month) for year in plan["temporal_design"]["seasons"] for month in MONTHS]
    if expected != date_grid():
        raise RuntimeError("The frozen prediction seasons must match May through July 2012 through 2026")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    for year, month in expected:
        url = source_url(year, month)
        output = output_path(year, month)
        if year == 2026 and month == 7:
            source = AUDIT_RAW
            if not source.exists():
                raise FileNotFoundError(f"The audited July 2026 source is absent: {source}")
            status = "audited_existing_file"
        elif output.exists():
            source = output
            status = "cached"
        else:
            retrieve(url, output)
            source = output
            status = "retrieved"
        record = {
            "year": year,
            "month": month,
            "url": url,
            "file": str(source.relative_to(ROOT)).replace("\\", "/"),
            "bytes": source.stat().st_size,
            "sha256": sha256(source),
            "status": status,
        }
        records.append(record)
        print(json.dumps({"year": year, "month": month, "status": status, "bytes": record["bytes"]}, ensure_ascii=False), flush=True)
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "script": Path(__file__).name,
        "plan": str(PLAN_PATH.relative_to(ROOT)).replace("\\", "/"),
        "product": "CHIRPS v3 monthly global COG",
        "records": records,
        "source_limit": "Files are global grids and preserve provider values. A later audit must check georeferencing, nodata, pixel aggregation and monthly completeness before analytical use.",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(MANIFEST.relative_to(ROOT)), "file_count": len(records), "total_bytes": sum(row["bytes"] for row in records)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
