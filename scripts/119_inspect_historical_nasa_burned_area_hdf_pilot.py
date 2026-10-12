"""Inspect layer structure of the authenticated 2018 NASA burned-area pilot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pyhdf.SD import SD, SDC


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "nasa" / ".burned_area_historical_2026-09-07.partial"
OUTPUT = ROOT / "results" / "exploratory" / "inspect_historical_nasa_burned_area_hdf_pilot_2018_2026-09-07.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def inspect(path: Path) -> dict[str, object]:
    handle = SD(str(path), SDC.READ)
    try:
        layers = []
        for name, details in handle.datasets().items():
            dimension_names, dimensions, numeric_type, index = details
            dataset = handle.select(index)
            try:
                attributes = dataset.attributes()
                retained_attributes = {
                    key: attributes[key]
                    for key in ("long_name", "units", "valid_range", "_FillValue", "scale_factor", "add_offset")
                    if key in attributes
                }
                layers.append(
                    {
                        "name": name,
                        "dimension_names": list(dimension_names),
                        "dimensions": list(dimensions),
                        "numeric_type": numeric_type,
                        "dataset_index": index,
                        "selected_attributes": retained_attributes,
                    }
                )
            finally:
                dataset.endaccess()
        return {
            "file": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "global_attribute_names": sorted(handle.attributes()),
            "layers": layers,
        }
    finally:
        handle.end()


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {OUTPUT}")
    paths = sorted(RAW_DIR.glob("*.hdf"))
    if len(paths) != 2:
        raise RuntimeError(f"Expected exactly two pilot HDF files, found {len(paths)}")
    output = {
        "audit_date": "2026-09-07",
        "inference_class": "descriptive file-schema audit",
        "files": [inspect(path) for path in paths],
        "interpretation": "Layer names and metadata establish file structure only. Pixel quality and agreement with GWIS remain untested.",
    }
    OUTPUT.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(output, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
