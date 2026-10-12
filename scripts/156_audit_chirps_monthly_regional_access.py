"""Audit one CHIRPS v3 monthly COG for the regional antecedent design.

The source is tested as an alternative precipitation block after CDS rejected
large ERA5 Land selections and did not return a monthly request within the
predeclared waiting limit. This test downloads one month only and verifies both
the file container and local pixel decoding before any historical series is
retrieved.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

import tifffile


ROOT = Path(__file__).resolve().parents[1]
URL = "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/monthly/global/cogs/chirps-v3.0.2026.07.cog"
RAW_PATH = ROOT / "data" / "raw" / "chirps_v3" / "chirps-v3.0.2026.07.monthly.global.cog"
AUDIT_PATH = ROOT / "results" / "exploratory" / "audit_chirps_monthly_regional_access_v1.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def download() -> str:
    if RAW_PATH.exists():
        return "cached"
    partial = RAW_PATH.with_suffix(RAW_PATH.suffix + ".partial")
    if partial.exists():
        raise FileExistsError(f"Incomplete CHIRPS retrieval needs inspection: {partial}")
    RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(URL, timeout=180) as response, partial.open("wb") as output:
        while block := response.read(1024 * 1024):
            output.write(block)
    partial.replace(RAW_PATH)
    return "retrieved"


def main() -> None:
    status = download()
    with tifffile.TiffFile(RAW_PATH) as container:
        page = container.pages[0]
        metadata = {
            "page_count": len(container.pages),
            "shape": list(page.shape),
            "dtype": str(page.dtype),
            "compression": str(page.compression),
            "is_tiled": bool(page.is_tiled),
            "tile_shape": list(page.tile) if page.is_tiled else None,
            "geotiff_tags": {tag.name: str(tag.value) for tag in page.tags.values() if tag.name in {"ModelPixelScaleTag", "ModelTiepointTag", "GeoKeyDirectoryTag", "GDAL_NODATA"}},
        }
        try:
            values = page.asarray()
            decoding = {"passed": True, "shape": list(values.shape), "minimum": float(values.min()), "maximum": float(values.max())}
        except Exception as error:
            decoding = {"passed": False, "error_type": type(error).__name__, "message": str(error)}
    audit = {
        "script": Path(__file__).name,
        "url": URL,
        "retrieval_status": status,
        "raw_file": str(RAW_PATH.relative_to(ROOT)).replace("\\", "/"),
        "bytes": RAW_PATH.stat().st_size,
        "sha256": sha256(RAW_PATH),
        "metadata": metadata,
        "decoding": decoding,
        "acceptance": {
            "primary_page_has_two_spatial_dimensions": len(metadata["shape"]) == 2,
            "overview_pages_allowed": metadata["page_count"] >= 1,
            "local_pixel_decoding_passed": decoding["passed"],
        },
        "limit": "A successfully decoded global COG does not establish regional aggregation, month completeness, precipitation units, version consistency or a historical source series.",
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"bytes": audit["bytes"], "metadata": metadata, "decoding": decoding, "acceptance": audit["acceptance"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
