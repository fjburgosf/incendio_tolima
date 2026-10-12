"""Apply the frozen SCL date rule to the stratified GWIS event sample."""

from __future__ import annotations

import importlib.util
import io
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import tifffile
from shapely import contains_xy
from shapely.geometry import shape


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts" / "78_screen_sentinel2_scl_for_largest_gwis_event.py"
SAMPLE_SOURCE = ROOT / "results" / "exploratory" / "define_gwis_sentinel_validation_sample.json"
EVENT_SOURCE = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_august_2026.geojson"
LARGEST_SCREEN = ROOT / "results" / "exploratory" / "screen_sentinel2_scl_gwis_event_15606221.json"
RAW_DIR = ROOT / "data" / "raw" / "sentinel2_gwis_validation_sample_scl_2026-09-06"
OUTPUT = ROOT / "results" / "exploratory" / "screen_sentinel2_scl_gwis_validation_sample.json"


def load_helper():
    spec = importlib.util.spec_from_file_location("sentinel_scl_helpers", HELPER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load script 78 helper functions")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evaluate_date(helper, token, event_id, event_geometry, bbox, width, height, day, period, event_dir):
    payload, body, content_type = helper.download_scl(token, bbox, width, height, day)
    tif_path = event_dir / f"sentinel2_scl_event_{event_id}_{day.isoformat()}.tif"
    manifest_path = event_dir / f"sentinel2_scl_event_{event_id}_{day.isoformat()}_manifest.json"
    tif_path.write_bytes(payload)
    image = tifffile.imread(io.BytesIO(payload))
    if image.shape != (height, width, 2):
        raise RuntimeError(f"Unexpected SCL raster shape for event {event_id} on {day}: {image.shape}")
    xs = bbox[0] + (np.arange(width) + 0.5) / width * (bbox[2] - bbox[0])
    ys = bbox[3] - (np.arange(height) + 0.5) / height * (bbox[3] - bbox[1])
    inside = contains_xy(event_geometry, xs[np.newaxis, :], ys[:, np.newaxis])
    scl = np.rint(image[:, :, 0]).astype(int)
    data_mask = image[:, :, 1] > 0
    usable = inside & data_mask & np.isin(scl, list(helper.USABLE_SCL))
    inside_count = int(inside.sum())
    if inside_count == 0:
        raise RuntimeError(f"Event {event_id} has no pixel centres in SCL raster")
    fraction = float(usable.sum() / inside_count)
    observation = {
        "date": day.isoformat(),
        "period": period,
        "event_pixel_count": inside_count,
        "usable_pixel_count": int(usable.sum()),
        "usable_fraction": fraction,
        "qualifies": fraction >= helper.MIN_USABLE_FRACTION,
        "scl_counts_inside_event": dict(
            sorted(Counter(int(value) for value in scl[inside].ravel()).items())
        ),
        "source_file": str(tif_path.relative_to(ROOT)),
        "sha256": helper.sha256(tif_path),
    }
    manifest_path.write_text(
        json.dumps(
            {
                "source": "Copernicus Data Space Process API",
                "collection": "sentinel-2-l2a",
                "request": body,
                "file": tif_path.name,
                "bytes": len(payload),
                "sha256": observation["sha256"],
                "content_type": content_type,
                "purpose": "Sequential SCL screen under rule frozen in script 81",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return observation


def main() -> None:
    helper = load_helper()
    for target in (RAW_DIR, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {target}")
    with SAMPLE_SOURCE.open(encoding="utf-8") as handle:
        sample = json.load(handle)
    with EVENT_SOURCE.open(encoding="utf-8") as handle:
        event_collection = json.load(handle)
    with LARGEST_SCREEN.open(encoding="utf-8") as handle:
        largest_screen = json.load(handle)
    event_features = {
        feature["properties"]["fire_id"]: feature for feature in event_collection["features"]
    }
    token = helper.access_token(helper.read_env(helper.ENV_FILE))
    RAW_DIR.mkdir(parents=True)
    results = []

    for selected in sample["selected_events"]:
        event_id = selected["fire_id"]
        if selected["already_audited"]:
            results.append(
                {
                    "fire_id": event_id,
                    "area_stratum": selected["area_stratum"],
                    "reused_existing_screen": str(LARGEST_SCREEN.relative_to(ROOT)),
                    "selected_pre_date": largest_screen["selected_pre_date"],
                    "selected_post_date": largest_screen["selected_post_date"],
                    "selection_gate_passed": largest_screen["selection_gate_passed"],
                    "attempted_dates": largest_screen["observations"],
                }
            )
            continue

        feature = event_features[event_id]
        event_geometry = shape(feature["geometry"])
        west, south, east, north = event_geometry.bounds
        bbox = (
            west - helper.PADDING_DEGREES,
            south - helper.PADDING_DEGREES,
            east + helper.PADDING_DEGREES,
            north + helper.PADDING_DEGREES,
        )
        width, height = helper.raster_dimensions(bbox)
        event_dir = RAW_DIR / event_id
        event_dir.mkdir()
        catalogue, catalogue_url = helper.catalogue_query(token, bbox)
        catalogue_path = event_dir / "cdse_odata_catalogue_response.json"
        catalogue_path.write_text(
            json.dumps(catalogue, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        products = catalogue.get("value", [])
        dates = sorted(
            {
                datetime.fromisoformat(
                    product["ContentDate"]["Start"].replace("Z", "+00:00")
                ).date()
                for product in products
                if product.get("ContentDate", {}).get("Start")
            }
        )
        initial_date = datetime.strptime(selected["initial_datetime"], "%Y-%m-%d %H:%M:%S").date()
        final_date = datetime.strptime(selected["final_datetime"], "%Y-%m-%d %H:%M:%S").date()
        period_candidates = {
            "pre": sorted((day for day in dates if day < initial_date), reverse=True),
            "post": sorted(day for day in dates if day > final_date),
        }
        observations = []
        selected_dates = {"pre": None, "post": None}
        for period in ("pre", "post"):
            for day in period_candidates[period]:
                observation = evaluate_date(
                    helper,
                    token,
                    event_id,
                    event_geometry,
                    bbox,
                    width,
                    height,
                    day,
                    period,
                    event_dir,
                )
                observations.append(observation)
                if observation["qualifies"]:
                    selected_dates[period] = day.isoformat()
                    break
        results.append(
            {
                "fire_id": event_id,
                "area_stratum": selected["area_stratum"],
                "bbox_wgs84_with_padding": list(bbox),
                "raster_shape": [height, width],
                "catalogue_url_without_credentials": catalogue_url,
                "catalogue_response": str(catalogue_path.relative_to(ROOT)),
                "catalogue_sha256": helper.sha256(catalogue_path),
                "catalogue_product_count": len(products),
                "catalogue_dates": [day.isoformat() for day in dates],
                "selected_pre_date": selected_dates["pre"],
                "selected_post_date": selected_dates["post"],
                "selection_gate_passed": all(selected_dates.values()),
                "attempted_dates": sorted(observations, key=lambda item: item["date"]),
            }
        )

    output = {
        "audit_date": "2026-09-06",
        "sample_definition": str(SAMPLE_SOURCE.relative_to(ROOT)),
        "rule_applied_without_change": sample["sentinel_rule_to_apply_without_change"],
        "event_count": len(results),
        "events_passing_pre_and_post_selection": sum(
            result["selection_gate_passed"] for result in results
        ),
        "events": results,
        "interpretation_limit": (
            "The sequential search stops at the closest qualifying acquisition on each side. "
            "Failure remains failure and does not trigger relaxation of the 80 percent threshold."
        ),
    }
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
