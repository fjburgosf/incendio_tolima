"""Validate the frozen GWIS sample with Sentinel-2 NBR and full control rings."""

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
SAMPLE_SOURCE = ROOT / "results" / "exploratory" / "define_gwis_sentinel_validation_sample.json"
SCREEN_SOURCE = ROOT / "results" / "exploratory" / "screen_sentinel2_scl_gwis_validation_sample.json"
EVENT_SOURCE = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_august_2026.geojson"
LARGEST_RESULT = ROOT / "results" / "exploratory" / "validate_gwis_event_15606221_with_sentinel2_nbr_v2.json"
RAW_DIR = ROOT / "data" / "raw" / "sentinel2_gwis_validation_sample_nbr_2026-09-06"
INTERIM_DIR = ROOT / "data" / "interim" / "sentinel2_gwis_validation_sample_nbr"
OUTPUT = ROOT / "results" / "exploratory" / "validate_gwis_sample_with_sentinel2_nbr.json"
PIXEL_SIZE_METRES = 20.0
DOMAIN_PADDING_DEGREES = 0.001


def load_helper():
    spec = importlib.util.spec_from_file_location("sentinel_nbr_helpers", HELPER_PATH)
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


def validate_event(helper, token, screen, feature, all_events_utm):
    event_id = screen["fire_id"]
    pre_day = date.fromisoformat(screen["selected_pre_date"])
    post_day = date.fromisoformat(screen["selected_post_date"])
    event_geometry = shape(feature["geometry"])
    to_utm = Transformer.from_crs(helper.EPSG_WGS84, helper.EPSG_UTM18N, always_xy=True).transform
    to_wgs84 = Transformer.from_crs(helper.EPSG_UTM18N, helper.EPSG_WGS84, always_xy=True).transform
    event_utm = transform(to_utm, event_geometry)
    ring_utm = event_utm.buffer(helper.OUTER_RING_DISTANCE_METRES).difference(
        event_utm.buffer(helper.INNER_RING_DISTANCE_METRES)
    ).difference(all_events_utm)
    if ring_utm.is_empty:
        return {"fire_id": event_id, "status": "blocked", "reason": "complete control ring is empty"}
    control_ring = transform(to_wgs84, ring_utm)
    west, south, east, north = event_geometry.union(control_ring).bounds
    bbox = [west - DOMAIN_PADDING_DEGREES, south - DOMAIN_PADDING_DEGREES,
            east + DOMAIN_PADDING_DEGREES, north + DOMAIN_PADDING_DEGREES]
    width, height = raster_dimensions(bbox)
    xs = bbox[0] + (np.arange(width) + 0.5) / width * (bbox[2] - bbox[0])
    ys = bbox[3] - (np.arange(height) + 0.5) / height * (bbox[3] - bbox[1])
    inside_event = contains_xy(event_geometry, xs[np.newaxis, :], ys[:, np.newaxis])
    inside_ring = contains_xy(control_ring, xs[np.newaxis, :], ys[:, np.newaxis])
    if not inside_event.any() or not inside_ring.any():
        return {"fire_id": event_id, "status": "blocked", "reason": "event or ring has no pixel centres"}

    event_dir = RAW_DIR / event_id
    event_dir.mkdir(parents=True)
    arrays, sources = {}, []
    for label, day in (("pre", pre_day), ("post", post_day)):
        body = helper.process_body(bbox, width, height, day)
        payload, content_type = helper.download(token, body)
        tif_path = event_dir / f"sentinel2_b08_b12_scl_event_{event_id}_{day.isoformat()}.tif"
        manifest_path = event_dir / f"sentinel2_b08_b12_scl_event_{event_id}_{day.isoformat()}_manifest.json"
        tif_path.write_bytes(payload)
        image = tifffile.imread(tif_path)
        if image.shape != (height, width, 4):
            raise RuntimeError(f"Unexpected raster shape for {event_id} on {day}: {image.shape}")
        arrays[label] = {
            "b08": image[:, :, 0].astype(np.float64),
            "b12": image[:, :, 1].astype(np.float64),
            "scl": np.rint(image[:, :, 2]).astype(int),
            "data_mask": image[:, :, 3] > 0,
        }
        manifest_path.write_text(json.dumps({
            "source": "Copernicus Data Space Process API",
            "collection": "sentinel-2-l2a", "request": body, "file": tif_path.name,
            "bytes": len(payload), "sha256": helper.sha256(tif_path),
            "content_type": content_type,
            "purpose": "Frozen-sample spectral validation with complete control ring",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        sources.append({"period": label, "date": day.isoformat(),
                        "file": str(tif_path.relative_to(ROOT)), "sha256": helper.sha256(tif_path)})

    valid_pre = arrays["pre"]["data_mask"] & np.isin(arrays["pre"]["scl"], list(helper.USABLE_SCL))
    valid_post = arrays["post"]["data_mask"] & np.isin(arrays["post"]["scl"], list(helper.USABLE_SCL))
    den_pre = arrays["pre"]["b08"] + arrays["pre"]["b12"]
    den_post = arrays["post"]["b08"] + arrays["post"]["b12"]
    joint = valid_pre & valid_post & (den_pre != 0) & (den_post != 0)
    nbr_pre = np.full((height, width), np.nan, dtype=np.float32)
    nbr_post = np.full((height, width), np.nan, dtype=np.float32)
    nbr_pre[joint] = ((arrays["pre"]["b08"] - arrays["pre"]["b12"])[joint] / den_pre[joint])
    nbr_post[joint] = ((arrays["post"]["b08"] - arrays["post"]["b12"])[joint] / den_post[joint])
    dnbr = nbr_pre - nbr_post
    event_joint, ring_joint = inside_event & joint, inside_ring & joint
    event_fraction = float(event_joint.sum() / inside_event.sum())
    ring_fraction = float(ring_joint.sum() / inside_ring.sum())
    event_summary, ring_summary = helper.summarize(dnbr[event_joint]), helper.summarize(dnbr[ring_joint])
    coverage_passed = event_fraction >= helper.MIN_JOINT_USABLE_FRACTION and ring_fraction >= helper.MIN_JOINT_USABLE_FRACTION
    direction_passed = bool(event_summary["median"] is not None and ring_summary["median"] is not None and event_summary["median"] > ring_summary["median"])
    array_path = INTERIM_DIR / f"event_{event_id}_nbr_arrays.npz"
    np.savez_compressed(array_path, nbr_pre=nbr_pre, nbr_post=nbr_post, dnbr=dnbr,
                        joint_valid=joint, inside_event=inside_event,
                        inside_complete_control_ring=inside_ring, bbox_wgs84=np.asarray(bbox))
    return {
        "fire_id": event_id, "area_stratum": screen["area_stratum"], "status": "evaluated",
        "pre_date": pre_day.isoformat(), "post_date": post_day.isoformat(),
        "bbox_wgs84_complete_domain": bbox, "raster_shape": [height, width],
        "coverage": {"event_pixel_centres": int(inside_event.sum()),
                     "event_joint_usable_pixels": int(event_joint.sum()),
                     "event_joint_usable_fraction": event_fraction,
                     "complete_control_ring_pixel_centres": int(inside_ring.sum()),
                     "complete_control_ring_joint_usable_pixels": int(ring_joint.sum()),
                     "complete_control_ring_joint_usable_fraction": ring_fraction,
                     "coverage_gate_passed": coverage_passed},
        "dnbr": {"event": event_summary, "complete_control_ring": ring_summary,
                 "median_difference_event_minus_ring": event_summary["median"] - ring_summary["median"] if event_summary["median"] is not None and ring_summary["median"] is not None else None,
                 "directional_check_passed": direction_passed},
        "validation_gate_passed": coverage_passed and direction_passed,
        "source_files": sources,
        "processed_arrays": {"file": str(array_path.relative_to(ROOT)), "sha256": helper.sha256(array_path)},
    }


def main() -> None:
    helper = load_helper()
    for target in (RAW_DIR, INTERIM_DIR, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {target}")
    sample = json.loads(SAMPLE_SOURCE.read_text(encoding="utf-8"))
    screen = json.loads(SCREEN_SOURCE.read_text(encoding="utf-8"))
    collection = json.loads(EVENT_SOURCE.read_text(encoding="utf-8"))
    largest = json.loads(LARGEST_RESULT.read_text(encoding="utf-8"))
    features = {feature["properties"]["fire_id"]: feature for feature in collection["features"]}
    to_utm = Transformer.from_crs(helper.EPSG_WGS84, helper.EPSG_UTM18N, always_xy=True).transform
    all_events_utm = transform(to_utm, unary_union([shape(feature["geometry"]) for feature in collection["features"]]))
    RAW_DIR.mkdir(parents=True)
    INTERIM_DIR.mkdir(parents=True)
    token = helper.access_token(helper.read_env(helper.ENV_FILE))
    results = []
    for item in screen["events"]:
        event_id = item["fire_id"]
        if not item["selection_gate_passed"]:
            results.append({"fire_id": event_id, "area_stratum": item["area_stratum"],
                            "status": "not_evaluated", "reason": "SCL pre and post selection gate failed"})
        elif event_id == "15606221":
            results.append({"fire_id": event_id, "area_stratum": item["area_stratum"],
                            "status": "reused_corrected_audit", "source": str(LARGEST_RESULT.relative_to(ROOT)),
                            "pre_date": largest["pre_date"], "post_date": largest["post_date"],
                            "coverage": largest["coverage"], "dnbr": largest["dnbr"],
                            "validation_gate_passed": largest["validation_gate_passed"]})
        else:
            result = validate_event(helper, token, item, features[event_id], all_events_utm)
            results.append(result)
            print(event_id, result.get("validation_gate_passed"), result.get("dnbr", {}).get("median_difference_event_minus_ring"))
    evaluated = [item for item in results if item["status"] in {"evaluated", "reused_corrected_audit"}]
    output = {
        "audit_date": "2026-09-06", "inference_class": "measurement validation audit",
        "sample_definition": str(SAMPLE_SOURCE.relative_to(ROOT)),
        "scl_screen": str(SCREEN_SOURCE.relative_to(ROOT)),
        "predeclared_rule_applied_without_change": sample["sentinel_rule_to_apply_without_change"],
        "sample_event_count": len(results), "scl_gate_pass_count": len(evaluated),
        "validation_gate_pass_count": sum(bool(item.get("validation_gate_passed")) for item in evaluated),
        "events": results,
        "interpretation_limit": "This is external Sentinel spectral corroboration conditioned on GWIS geometries. It does not validate every boundary, classify severity, establish cause, or make GWIS independent of FIRMS.",
    }
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: output[key] for key in ("sample_event_count", "scl_gate_pass_count", "validation_gate_pass_count")}, indent=2))


if __name__ == "__main__":
    main()
