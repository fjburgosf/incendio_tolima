"""Corrected Sentinel-2 NBR validation with the full 2 km control ring.

This supersedes script 79, whose raster domain truncated the predeclared ring.
Dates and validation thresholds are unchanged. The corrected bounding box is
derived from the complete event plus mapped-fire-free control geometry.
"""

from __future__ import annotations

import importlib.util
import json
import math
from datetime import date
from pathlib import Path

import numpy as np
import tifffile
from pyproj import Transformer
from shapely import contains_xy
from shapely.geometry import shape
from shapely.ops import transform, unary_union


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts" / "79_validate_largest_gwis_event_with_sentinel2_nbr.py"
RAW_DIR = ROOT / "data" / "raw" / "sentinel2_gwis_event_15606221_nbr_v2_2026-09-06"
INTERIM = ROOT / "data" / "interim" / "sentinel2_gwis_event_15606221_nbr_arrays_v2.npz"
OUTPUT = ROOT / "results" / "exploratory" / "validate_gwis_event_15606221_with_sentinel2_nbr_v2.json"
SUPERSEDED_OUTPUT = ROOT / "results" / "exploratory" / "validate_gwis_event_15606221_with_sentinel2_nbr.json"
PIXEL_SIZE_METRES = 20.0
DOMAIN_PADDING_DEGREES = 0.001


def load_helper():
    spec = importlib.util.spec_from_file_location("sentinel_nbr_v1_helpers", HELPER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load script 79 helper functions")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def raster_dimensions(bbox: list[float]) -> tuple[int, int]:
    west, south, east, north = bbox
    latitude = (south + north) / 2
    width_metres = (east - west) * 111_320 * math.cos(math.radians(latitude))
    height_metres = (north - south) * 110_574
    return max(1, math.ceil(width_metres / PIXEL_SIZE_METRES)), max(
        1, math.ceil(height_metres / PIXEL_SIZE_METRES)
    )


def main() -> None:
    helper = load_helper()
    for target in (RAW_DIR, INTERIM, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {target}")
    with helper.SCL_SCREEN.open(encoding="utf-8") as handle:
        screen = json.load(handle)
    if not screen["selection_gate_passed"]:
        raise RuntimeError("SCL selection gate did not pass")
    pre_day = date.fromisoformat(screen["selected_pre_date"])
    post_day = date.fromisoformat(screen["selected_post_date"])

    with helper.EVENT_SOURCE.open(encoding="utf-8") as handle:
        collection = json.load(handle)
    event_feature = next(
        feature
        for feature in collection["features"]
        if feature["properties"]["fire_id"] == helper.FIRE_ID
    )
    event_geometry = shape(event_feature["geometry"])
    all_event_geometry = unary_union([shape(feature["geometry"]) for feature in collection["features"]])
    to_utm = Transformer.from_crs(helper.EPSG_WGS84, helper.EPSG_UTM18N, always_xy=True).transform
    to_wgs84 = Transformer.from_crs(helper.EPSG_UTM18N, helper.EPSG_WGS84, always_xy=True).transform
    event_utm = transform(to_utm, event_geometry)
    all_events_utm = transform(to_utm, all_event_geometry)
    complete_ring_utm = event_utm.buffer(helper.OUTER_RING_DISTANCE_METRES).difference(
        event_utm.buffer(helper.INNER_RING_DISTANCE_METRES)
    )
    complete_ring_utm = complete_ring_utm.difference(all_events_utm)
    control_ring = transform(to_wgs84, complete_ring_utm)
    domain = event_geometry.union(control_ring)
    west, south, east, north = domain.bounds
    bbox = [
        west - DOMAIN_PADDING_DEGREES,
        south - DOMAIN_PADDING_DEGREES,
        east + DOMAIN_PADDING_DEGREES,
        north + DOMAIN_PADDING_DEGREES,
    ]
    width, height = raster_dimensions(bbox)

    xs = bbox[0] + (np.arange(width) + 0.5) / width * (bbox[2] - bbox[0])
    ys = bbox[3] - (np.arange(height) + 0.5) / height * (bbox[3] - bbox[1])
    inside_event = contains_xy(event_geometry, xs[np.newaxis, :], ys[:, np.newaxis])
    inside_ring = contains_xy(control_ring, xs[np.newaxis, :], ys[:, np.newaxis])
    if not inside_event.any() or not inside_ring.any():
        raise RuntimeError("Event or complete control ring has no raster pixel centres")

    token = helper.access_token(helper.read_env(helper.ENV_FILE))
    RAW_DIR.mkdir(parents=True)
    arrays = {}
    source_records = []
    for label, day in (("pre", pre_day), ("post", post_day)):
        body = helper.process_body(bbox, width, height, day)
        payload, content_type = helper.download(token, body)
        tif_path = RAW_DIR / f"sentinel2_b08_b12_scl_event_{helper.FIRE_ID}_{day.isoformat()}.tif"
        manifest_path = RAW_DIR / f"sentinel2_b08_b12_scl_event_{helper.FIRE_ID}_{day.isoformat()}_manifest.json"
        tif_path.write_bytes(payload)
        image = tifffile.imread(tif_path)
        if image.shape != (height, width, 4):
            raise RuntimeError(f"Unexpected raster shape for {day}: {image.shape}")
        arrays[label] = {
            "b08": image[:, :, 0].astype(np.float64),
            "b12": image[:, :, 1].astype(np.float64),
            "scl": np.rint(image[:, :, 2]).astype(int),
            "data_mask": image[:, :, 3] > 0,
        }
        manifest_path.write_text(
            json.dumps(
                {
                    "source": "Copernicus Data Space Process API",
                    "collection": "sentinel-2-l2a",
                    "request": body,
                    "file": tif_path.name,
                    "bytes": len(payload),
                    "sha256": helper.sha256(tif_path),
                    "content_type": content_type,
                    "purpose": "Corrected validation using the complete predeclared control ring",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        source_records.append(
            {
                "period": label,
                "date": day.isoformat(),
                "file": str(tif_path.relative_to(ROOT)),
                "sha256": helper.sha256(tif_path),
            }
        )

    valid_pre = arrays["pre"]["data_mask"] & np.isin(
        arrays["pre"]["scl"], list(helper.USABLE_SCL)
    )
    valid_post = arrays["post"]["data_mask"] & np.isin(
        arrays["post"]["scl"], list(helper.USABLE_SCL)
    )
    denominator_pre = arrays["pre"]["b08"] + arrays["pre"]["b12"]
    denominator_post = arrays["post"]["b08"] + arrays["post"]["b12"]
    joint_valid = valid_pre & valid_post & (denominator_pre != 0) & (denominator_post != 0)
    nbr_pre = np.full((height, width), np.nan, dtype=np.float32)
    nbr_post = np.full((height, width), np.nan, dtype=np.float32)
    nbr_pre[joint_valid] = (
        (arrays["pre"]["b08"] - arrays["pre"]["b12"])[joint_valid]
        / denominator_pre[joint_valid]
    )
    nbr_post[joint_valid] = (
        (arrays["post"]["b08"] - arrays["post"]["b12"])[joint_valid]
        / denominator_post[joint_valid]
    )
    dnbr = nbr_pre - nbr_post
    event_joint = inside_event & joint_valid
    ring_joint = inside_ring & joint_valid
    event_fraction = float(event_joint.sum() / inside_event.sum())
    ring_fraction = float(ring_joint.sum() / inside_ring.sum())
    event_summary = helper.summarize(dnbr[event_joint])
    ring_summary = helper.summarize(dnbr[ring_joint])
    coverage_passed = (
        event_fraction >= helper.MIN_JOINT_USABLE_FRACTION
        and ring_fraction >= helper.MIN_JOINT_USABLE_FRACTION
    )
    direction_passed = bool(
        event_summary["median"] is not None
        and ring_summary["median"] is not None
        and event_summary["median"] > ring_summary["median"]
    )

    INTERIM.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        INTERIM,
        nbr_pre=nbr_pre,
        nbr_post=nbr_post,
        dnbr=dnbr,
        joint_valid=joint_valid,
        inside_event=inside_event,
        inside_complete_control_ring=inside_ring,
        bbox_wgs84=np.asarray(bbox),
    )
    result = {
        "audit_date": "2026-09-06",
        "inference_class": "measurement validation audit",
        "fire_id": helper.FIRE_ID,
        "supersedes": {
            "output": str(SUPERSEDED_OUTPUT.relative_to(ROOT)),
            "reason": (
                "The first raster domain truncated the 250 to 2000 metre control ring. "
                "Its comparison is invalid and must not be cited."
            ),
            "dates_or_thresholds_changed": False,
        },
        "pre_date": pre_day.isoformat(),
        "post_date": post_day.isoformat(),
        "bbox_wgs84_complete_domain": bbox,
        "raster_shape": [height, width],
        "predeclared_validation_rule": {
            "usable_scl_classes": sorted(helper.USABLE_SCL),
            "minimum_joint_usable_fraction_in_event_and_ring": helper.MIN_JOINT_USABLE_FRACTION,
            "control_ring_metres": [
                helper.INNER_RING_DISTANCE_METRES,
                helper.OUTER_RING_DISTANCE_METRES,
            ],
            "control_excludes_all_gwis_event_polygons": True,
            "directional_check": "median dNBR inside event greater than median dNBR in ring",
            "no_burn_severity_threshold": True,
        },
        "coverage": {
            "event_pixel_centres": int(inside_event.sum()),
            "event_joint_usable_pixels": int(event_joint.sum()),
            "event_joint_usable_fraction": event_fraction,
            "complete_control_ring_pixel_centres": int(inside_ring.sum()),
            "complete_control_ring_joint_usable_pixels": int(ring_joint.sum()),
            "complete_control_ring_joint_usable_fraction": ring_fraction,
            "coverage_gate_passed": coverage_passed,
        },
        "dnbr": {
            "definition": "NBR_pre minus NBR_post, with NBR equal to B08 minus B12 divided by B08 plus B12",
            "event": event_summary,
            "complete_control_ring": ring_summary,
            "median_difference_event_minus_ring": (
                event_summary["median"] - ring_summary["median"]
                if event_summary["median"] is not None and ring_summary["median"] is not None
                else None
            ),
            "directional_check_passed": direction_passed,
        },
        "validation_gate_passed": coverage_passed and direction_passed,
        "source_files": source_records,
        "processed_arrays": {
            "file": str(INTERIM.relative_to(ROOT)),
            "sha256": helper.sha256(INTERIM),
        },
        "interpretation": (
            "The corrected check compares the event against the complete available ring. "
            "Passing supports an external Sentinel-2 spectral change signal conditioned on the "
            "GWIS geometry. It does not validate every boundary pixel, classify severity, "
            "establish cause, or turn GWIS into a source independent of FIRMS."
        ),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
