"""Download and audit selected historic NASA burned-area granules for Tolima.

The Earthdata token is requested from a no-echo prompt and is never written to
the raw manifest, audit JSON, code, or console output.  The script is resumable
from its partial directory and will not overwrite an existing raw file.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
ACCESS_DATE = "2026-09-07"
CMR_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"
BBOX = "-76.2,2.0,-74.0,6.2"
TARGET_YEARS = (2018, 2020, 2022, 2024, 2025)
PRODUCTS = (("MCD64A1", "061"), ("VNP64A1", "002"))
RAW_DIR = ROOT / "data" / "raw" / "nasa" / f"burned_area_historical_{ACCESS_DATE}"
PARTIAL_DIR = ROOT / "data" / "raw" / "nasa" / f".burned_area_historical_{ACCESS_DATE}.partial"
OUTPUT = ROOT / "results" / "exploratory" / f"audit_historical_nasa_burned_area_download_{ACCESS_DATE}.json"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", nargs="+", type=int, choices=TARGET_YEARS, default=list(TARGET_YEARS))
    parser.add_argument("--earthdata-token-prompt", action="store_true")
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def query_granule(product: str, version: str, year: int) -> dict[str, object]:
    response = requests.get(
        CMR_URL,
        params={
            "short_name": product,
            "version": version,
            "temporal": f"{year}-08-01T00:00:00Z,{year}-09-03T00:00:00Z",
            "bounding_box": BBOX,
            "page_size": 20,
        },
        timeout=(20, 90),
        headers={"Accept": "application/json", "User-Agent": "tolima-fire-data-audit/1.0"},
    )
    response.raise_for_status()
    matches = [
        item for item in response.json().get("feed", {}).get("entry", [])
        if str(item.get("time_start", "")).startswith(f"{year}-08-")
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one August-starting {product} granule for {year}, found {len(matches)}")
    granule = matches[0]
    data_links = [
        link["href"] for link in granule.get("links", [])
        if link.get("href", "").lower().endswith(".hdf")
        and link.get("rel", "").endswith("/data#")
    ]
    if len(data_links) != 1:
        raise RuntimeError(f"Expected one downloadable data link for {granule.get('title')}")
    return {
        "product": product,
        "version": version,
        "year": year,
        "granule_id": granule.get("title"),
        "time_start": granule.get("time_start"),
        "time_end": granule.get("time_end"),
        "data_url": data_links[0],
    }


def download_granule(granule: dict[str, object], token: str) -> dict[str, object]:
    filename = str(granule["granule_id"]) + ".hdf"
    target = PARTIAL_DIR / filename
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite existing raw file: {target}")
    with requests.get(
        str(granule["data_url"]),
        headers={"Authorization": f"Bearer {token}", "User-Agent": "tolima-fire-data-audit/1.0"},
        stream=True,
        timeout=(30, 300),
    ) as response:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
    if target.stat().st_size < 1024:
        target.unlink()
        raise RuntimeError("Downloaded object was implausibly small and was not retained")
    return {
        "product": granule["product"],
        "version": granule["version"],
        "year": granule["year"],
        "granule_id": granule["granule_id"],
        "time_start": granule["time_start"],
        "time_end": granule["time_end"],
        "file": filename,
        "bytes": target.stat().st_size,
        "sha256": sha256(target),
        "content_type": content_type,
        "data_url_without_credentials": str(granule["data_url"]),
    }


def main() -> None:
    args = parse_arguments()
    if not args.earthdata_token_prompt:
        raise ValueError("Use --earthdata-token-prompt so the token is never put in command history")
    token = getpass.getpass("NASA Earthdata token: ").strip()
    if not token:
        raise ValueError("No Earthdata token received")
    if RAW_DIR.exists() or OUTPUT.exists():
        raise FileExistsError("A final historical NASA download already exists and will not be overwritten")
    PARTIAL_DIR.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    for year in args.years:
        for product, version in PRODUCTS:
            granule = query_granule(product, version, year)
            entries.append(download_granule(granule, token))
            print(f"Downloaded {product} {year}", flush=True)
    progress = {
        "accessed_at_utc": datetime.now(timezone.utc).isoformat(),
        "downloaded_in_this_run": entries,
        "credential_handling": "Earthdata token provided through no-echo prompt and not stored.",
    }
    suffix = "_".join(str(year) for year in args.years)
    (PARTIAL_DIR / f"manifest_run_{suffix}.json").write_text(json.dumps(progress, indent=2), encoding="utf-8")
    print(json.dumps({"downloaded": len(entries), "partial_directory": str(PARTIAL_DIR.relative_to(ROOT))}, indent=2))


if __name__ == "__main__":
    main()
