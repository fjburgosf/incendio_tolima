"""Diagnose UPRA fragments lost by the exact Cortolima boundary clip."""

from __future__ import annotations

import glob
import hashlib
import json
from collections import Counter
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely import make_valid
from shapely.geometry import shape
from shapely.ops import transform, unary_union


ROOT = Path(__file__).resolve().parents[1]
RAW_PATTERN = str(ROOT / "data" / "raw" / "upra" / "territorial_layers_tolima_2026-09-06_v2" / "frontier_features_page_*.geojson")
BOUNDARY_SOURCE = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
OUTPUT = ROOT / "results" / "exploratory" / "diagnose_upra_frontier_tolima_boundary_mismatch.json"
TO_UTM18N = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True).transform
MIN_AREA_HA = 0.01


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def area_ha(geometry) -> float:
    return float(geometry.area / 10_000.0) if not geometry.is_empty else 0.0


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {OUTPUT}")
    page_paths = [Path(path) for path in sorted(glob.glob(RAW_PATTERN))]
    boundary_collection = json.loads(BOUNDARY_SOURCE.read_text(encoding="utf-8-sig"))
    tolima_wgs84 = unary_union([make_valid(shape(feature["geometry"])) for feature in boundary_collection["features"]])
    tolima_utm = transform(TO_UTM18N, tolima_wgs84)

    failed = []
    input_count = 0
    invalid_count = 0
    for page_path in page_paths:
        payload = json.loads(page_path.read_text(encoding="utf-8"))
        for feature in payload["features"]:
            input_count += 1
            geometry = shape(feature["geometry"])
            if not geometry.is_valid:
                invalid_count += 1
                geometry = make_valid(geometry)
            geometry_utm = transform(TO_UTM18N, geometry)
            clipped_area = area_ha(geometry_utm.intersection(tolima_utm))
            if clipped_area < MIN_AREA_HA:
                properties = feature["properties"]
                distance_m = float(geometry_utm.distance(tolima_utm))
                failed.append({
                    "objectid": properties.get("objectid"),
                    "municipio": properties.get("municipio"),
                    "cod_dane_mpio": properties.get("cod_dane_mpio"),
                    "tipo_frontera": properties.get("tipo_frontera"),
                    "source_area_ha": properties.get("area_ha"),
                    "computed_original_area_ha": area_ha(geometry_utm),
                    "clipped_area_ha": clipped_area,
                    "distance_to_tolima_m": distance_m,
                    "bounds_wgs84": list(geometry.bounds),
                })

    distances = np.array([record["distance_to_tolima_m"] for record in failed], dtype=float)
    source_areas = np.array([float(record["source_area_ha"] or 0) for record in failed], dtype=float)
    computed_areas = np.array([record["computed_original_area_ha"] for record in failed], dtype=float)
    result = {
        "audit_date": "2026-09-06",
        "purpose": "diagnose the mechanism of fragments attributed to Tolima but below 0.01 ha after exact Cortolima clipping",
        "source_pages": {
            "count": len(page_paths),
            "files": [{"file": str(path.relative_to(ROOT)), "sha256": sha256(path)} for path in page_paths],
        },
        "boundary": {"file": str(BOUNDARY_SOURCE.relative_to(ROOT)), "sha256": sha256(BOUNDARY_SOURCE)},
        "input_feature_count": input_count,
        "invalid_input_geometry_count": invalid_count,
        "failed_clip_count": len(failed),
        "failed_clip_type_counts": dict(Counter(record["tipo_frontera"] for record in failed)),
        "failed_clip_municipality_counts": dict(sorted(Counter(record["municipio"] for record in failed).items())),
        "failed_source_area_ha_sum": float(source_areas.sum()),
        "failed_computed_original_area_ha_sum": float(computed_areas.sum()),
        "failed_source_area_ha_max": float(source_areas.max()) if len(source_areas) else None,
        "distance_to_cortolima_boundary_m": {
            "minimum": float(distances.min()) if len(distances) else None,
            "median": float(np.median(distances)) if len(distances) else None,
            "p95": float(np.percentile(distances, 95)) if len(distances) else None,
            "maximum": float(distances.max()) if len(distances) else None,
            "count_zero": int(np.sum(distances == 0)),
            "count_at_most_100": int(np.sum(distances <= 100)),
            "count_at_most_500": int(np.sum(distances <= 500)),
            "count_at_most_2000": int(np.sum(distances <= 2000)),
        },
        "largest_failed_fragments": sorted(failed, key=lambda record: record["computed_original_area_ha"], reverse=True)[:20],
        "interpretation_rule": (
            "A near-boundary pattern is compatible with differing cartographic boundaries and scales, but does not prove that mechanism. "
            "Fragments are not reassigned or buffered into Tolima. The exact intersection output remains the reproducible linkage."
        ),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "input_feature_count",
        "invalid_input_geometry_count",
        "failed_clip_count",
        "failed_source_area_ha_sum",
        "failed_computed_original_area_ha_sum",
        "distance_to_cortolima_boundary_m",
    )}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
