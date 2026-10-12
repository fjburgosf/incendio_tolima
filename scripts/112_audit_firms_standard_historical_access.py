"""Probe historical FIRMS Standard Processing access without persisting a MAP_KEY.

The audit makes deliberately small one-day requests in the established Tolima
screening box.  Successful CSV files are retained unchanged.  HTTP failures
and schema failures are recorded without retaining a request URL, response
body, or credential.  This establishes access and observed schema only.  It
does not establish a comparable event definition across years.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
ACCESS_DATE = "2026-09-07"
BBOX = "-76.2,2.0,-74.0,6.2"
API_TEMPLATE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{bbox}/{days}/{start}"
RAW_DIR = ROOT / "data" / "raw" / f"firms_standard_historical_access_probe_{ACCESS_DATE}"
PARTIAL_DIR = ROOT / "data" / "raw" / f".firms_standard_historical_access_probe_{ACCESS_DATE}.partial"
OUTPUT = ROOT / "results" / "exploratory" / f"audit_firms_standard_historical_access_{ACCESS_DATE}.json"

# The pair of VIIRS platforms exposes the intended satellite lineage.  SNPP is
# included only as an availability comparison, not as a substitute cohort.
PROBES = (
    ("VIIRS_NOAA20_SP", "2018-08-01"),
    ("VIIRS_NOAA20_SP", "2024-08-01"),
    ("VIIRS_NOAA20_SP", "2025-08-01"),
    ("VIIRS_NOAA20_SP", "2026-08-01"),
    ("VIIRS_NOAA21_SP", "2024-08-01"),
    ("VIIRS_NOAA21_SP", "2025-08-01"),
    ("VIIRS_NOAA21_SP", "2026-08-01"),
    ("VIIRS_SNPP_SP", "2018-08-01"),
    ("VIIRS_SNPP_SP", "2024-08-01"),
    ("VIIRS_SNPP_SP", "2025-08-01"),
    ("VIIRS_SNPP_SP", "2026-08-01"),
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--map-key-stdin",
        action="store_true",
        help="Read a MAP_KEY from standard input without recording it.",
    )
    parser.add_argument(
        "--map-key-prompt",
        action="store_true",
        help="Prompt without terminal echo and do not record the MAP_KEY.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def validate_csv(payload: bytes) -> tuple[int, list[str]]:
    text = payload.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("empty response")
    fields = rows[0]
    required = {"latitude", "longitude", "acq_date", "acq_time", "frp"}
    if not required.issubset(fields):
        raise ValueError("response lacks expected FIRMS CSV fields")
    return len(rows) - 1, fields


def fetch(map_key: str, source: str, start_date: str) -> bytes:
    url = API_TEMPLATE.format(
        key=map_key,
        source=source,
        bbox=BBOX,
        days=1,
        start=start_date,
    )
    try:
        with urlopen(url, timeout=120) as response:
            return response.read()
    except HTTPError as error:
        raise RuntimeError(f"HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError("connection failed") from error


def main() -> None:
    arguments = parse_arguments()
    if arguments.map_key_prompt:
        map_key = getpass.getpass("FIRMS MAP_KEY: ").strip()
    elif arguments.map_key_stdin:
        map_key = sys.stdin.readline().strip()
    else:
        raise ValueError("Use a nonpersistent MAP_KEY input mode")
    if not map_key:
        raise ValueError("No MAP_KEY was received on standard input")
    for target in (RAW_DIR, PARTIAL_DIR, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {target}")

    PARTIAL_DIR.mkdir(parents=True)
    entries: list[dict[str, object]] = []
    for source, start_date in PROBES:
        filename = f"{source.lower()}_{start_date}.csv"
        try:
            payload = fetch(map_key, source, start_date)
            record_count, fields = validate_csv(payload)
            target = PARTIAL_DIR / filename
            target.write_bytes(payload)
            entries.append(
                {
                    "source": source,
                    "start_date": start_date,
                    "day_range": 1,
                    "status": "success",
                    "file": filename,
                    "record_count": record_count,
                    "fields": fields,
                    "sha256": sha256_file(target),
                    "request_template": (
                        "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
                        "[REDACTED_MAP_KEY]/{source}/{bbox}/1/{start_date}"
                    ).format(source=source, bbox=BBOX, start_date=start_date),
                }
            )
        except (RuntimeError, UnicodeDecodeError, ValueError) as error:
            entries.append(
                {
                    "source": source,
                    "start_date": start_date,
                    "day_range": 1,
                    "status": "failed",
                    "error": str(error),
                    "request_template": (
                        "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
                        "[REDACTED_MAP_KEY]/{source}/{bbox}/1/{start_date}"
                    ).format(source=source, bbox=BBOX, start_date=start_date),
                }
            )

    manifest = {
        "access_date_utc": datetime.now(UTC).isoformat(),
        "bbox": BBOX,
        "entries": entries,
        "credential_storage": "MAP_KEY read from standard input and not stored in this manifest",
    }
    (PARTIAL_DIR / "download_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    PARTIAL_DIR.rename(RAW_DIR)

    successes = [entry for entry in entries if entry["status"] == "success"]
    audit = {
        "audit_date": ACCESS_DATE,
        "inference_class": "descriptive source-access audit",
        "source": {
            "api_family": "NASA FIRMS area CSV",
            "raw_directory": str(RAW_DIR.relative_to(ROOT)),
            "raw_manifest_sha256": sha256_file(RAW_DIR / "download_manifest.json"),
        },
        "probe_count": len(entries),
        "success_count": len(successes),
        "entries": entries,
        "interpretation_rule": (
            "A successful Standard Processing CSV establishes that the named FIRMS source "
            "returned a parseable one-day historical response. It does not establish full "
            "archive completeness, stable processing across time, or a fire-event outcome."
        ),
    }
    OUTPUT.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(audit, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
