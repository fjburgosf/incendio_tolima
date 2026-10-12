"""Test independent Sentinel-2 spectral change for the largest GWIS event.

Dates are inherited from the SCL-only screen. Before downloading B08 and B12,
the validation criterion is fixed as follows. At least 80 percent of pixel
centres must be jointly usable in the event and in a nearby control ring, and
median dNBR inside the event must exceed median dNBR in the ring. This is a
spectral corroboration check, not a severity classification or causal test.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import tifffile
from pyproj import Transformer
from shapely import contains_xy
from shapely.geometry import shape
from shapely.ops import transform, unary_union


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
EVENT_SOURCE = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_august_2026.geojson"
SCL_SCREEN = ROOT / "results" / "exploratory" / "screen_sentinel2_scl_gwis_event_15606221.json"
FIRE_ID = "15606221"
RAW_DIR = ROOT / "data" / "raw" / "sentinel2_gwis_event_15606221_nbr_2026-09-06"
INTERIM = ROOT / "data" / "interim" / "sentinel2_gwis_event_15606221_nbr_arrays.npz"
OUTPUT = ROOT / "results" / "exploratory" / "validate_gwis_event_15606221_with_sentinel2_nbr.json"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
USABLE_SCL = {4, 5, 6, 7}
MIN_JOINT_USABLE_FRACTION = 0.80
INNER_RING_DISTANCE_METRES = 250.0
OUTER_RING_DISTANCE_METRES = 2000.0
EPSG_WGS84 = "EPSG:4326"
EPSG_UTM18N = "EPSG:32618"


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def access_token(values: dict[str, str]) -> str:
    form = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": values["CDSE_CLIENT_ID"],
            "client_secret": values["CDSE_CLIENT_SECRET"],
        }
    ).encode("utf-8")
    request = Request(
        TOKEN_URL,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        return json.load(response)["access_token"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def process_body(bbox: list[float], width: int, height: int, day: date) -> dict:
    next_day = day + timedelta(days=1)
    return {
        "input": {
            "bounds": {
                "bbox": bbox,
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [
                {
                    "type": "sentinel-2-l2a",
                    "dataFilter": {
                        "timeRange": {
                            "from": f"{day.isoformat()}T00:00:00Z",
                            "to": f"{next_day.isoformat()}T00:00:00Z",
                        },
                        "mosaickingOrder": "leastCC",
                    },
                }
            ],
        },
        "output": {
            "width": width,
            "height": height,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": (
            '//VERSION=3\nfunction setup() { return { input: ["B08", "B12", "SCL", "dataMask"], '
            'output: { bands: 4, sampleType: "FLOAT32" } }; }\n'
            "function evaluatePixel(s) { return [s.B08, s.B12, s.SCL, s.dataMask]; }"
        ),
    }


def download(token: str, body: dict) -> tuple[bytes, str]:
    request = Request(
        PROCESS_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=180) as response:
        payload = response.read()
        content_type = response.headers.get_content_type()
    if content_type not in {"image/tiff", "image/tiff; application=geotiff"}:
        raise RuntimeError(f"Unexpected Process API content type: {content_type}")
    return payload, content_type


def summarize(values: np.ndarray) -> dict[str, float | int | None]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return {"count": 0, "mean": None, "median": None, "q25": None, "q75": None}
    return {
        "count": int(finite.size),
        "mean": float(np.mean(finite)),
        "median": float(np.median(finite)),
        "q25": float(np.quantile(finite, 0.25)),
        "q75": float(np.quantile(finite, 0.75)),
    }


def main() -> None:
    for target in (RAW_DIR, INTERIM, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {target}")
    with SCL_SCREEN.open(encoding="utf-8") as handle:
        screen = json.load(handle)
    if not screen["selection_gate_passed"]:
        raise RuntimeError("SCL selection gate did not pass")
    pre_day = date.fromisoformat(screen["selected_pre_date"])
    post_day = date.fromisoformat(screen["selected_post_date"])
    bbox = [float(value) for value in screen["bbox_wgs84_with_padding"]]
    width = int(screen["output_width"])
    height = int(screen["output_height"])

    with EVENT_SOURCE.open(encoding="utf-8") as handle:
        collection = json.load(handle)
    event_feature = next(
        feature for feature in collection["features"] if feature["properties"]["fire_id"] == FIRE_ID
    )
    event_geometry = shape(event_feature["geometry"])
    all_event_geometry = unary_union([shape(feature["geometry"]) for feature in collection["features"]])
    to_utm = Transformer.from_crs(EPSG_WGS84, EPSG_UTM18N, always_xy=True).transform
    to_wgs84 = Transformer.from_crs(EPSG_UTM18N, EPSG_WGS84, always_xy=True).transform
    event_utm = transform(to_utm, event_geometry)
    all_events_utm = transform(to_utm, all_event_geometry)
    ring_utm = event_utm.buffer(OUTER_RING_DISTANCE_METRES).difference(
        event_utm.buffer(INNER_RING_DISTANCE_METRES)
    )
    ring_utm = ring_utm.difference(all_events_utm)
    control_ring = transform(to_wgs84, ring_utm)

    xs = bbox[0] + (np.arange(width) + 0.5) / width * (bbox[2] - bbox[0])
    ys = bbox[3] - (np.arange(height) + 0.5) / height * (bbox[3] - bbox[1])
    inside_event = contains_xy(event_geometry, xs[np.newaxis, :], ys[:, np.newaxis])
    inside_ring = contains_xy(control_ring, xs[np.newaxis, :], ys[:, np.newaxis])
    if not inside_event.any() or not inside_ring.any():
        raise RuntimeError("Event or control ring has no raster pixel centres")

    token = access_token(read_env(ENV_FILE))
    RAW_DIR.mkdir(parents=True)
    arrays = {}
    source_records = []
    for label, day in (("pre", pre_day), ("post", post_day)):
        body = process_body(bbox, width, height, day)
        payload, content_type = download(token, body)
        tif_path = RAW_DIR / f"sentinel2_b08_b12_scl_event_{FIRE_ID}_{day.isoformat()}.tif"
        manifest_path = RAW_DIR / f"sentinel2_b08_b12_scl_event_{FIRE_ID}_{day.isoformat()}_manifest.json"
        tif_path.write_bytes(payload)
        image = tifffile.imread(io.BytesIO(payload))
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
                    "sha256": sha256(tif_path),
                    "content_type": content_type,
                    "purpose": "Independent spectral change check using dates selected from SCL only",
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
                "sha256": sha256(tif_path),
            }
        )

    valid_pre = arrays["pre"]["data_mask"] & np.isin(arrays["pre"]["scl"], list(USABLE_SCL))
    valid_post = arrays["post"]["data_mask"] & np.isin(arrays["post"]["scl"], list(USABLE_SCL))
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
    event_summary = summarize(dnbr[event_joint])
    ring_summary = summarize(dnbr[ring_joint])
    coverage_passed = (
        event_fraction >= MIN_JOINT_USABLE_FRACTION
        and ring_fraction >= MIN_JOINT_USABLE_FRACTION
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
        inside_control_ring=inside_ring,
        bbox_wgs84=np.asarray(bbox),
    )
    result = {
        "audit_date": "2026-09-06",
        "inference_class": "measurement validation audit",
        "fire_id": FIRE_ID,
        "date_selection_source": str(SCL_SCREEN.relative_to(ROOT)),
        "pre_date": pre_day.isoformat(),
        "post_date": post_day.isoformat(),
        "bbox_wgs84": bbox,
        "raster_shape": [height, width],
        "predeclared_validation_rule": {
            "usable_scl_classes": sorted(USABLE_SCL),
            "minimum_joint_usable_fraction_in_event_and_ring": MIN_JOINT_USABLE_FRACTION,
            "control_ring_metres": [INNER_RING_DISTANCE_METRES, OUTER_RING_DISTANCE_METRES],
            "control_excludes_all_gwis_event_polygons": True,
            "directional_check": "median dNBR inside event greater than median dNBR in ring",
            "no_burn_severity_threshold": True,
        },
        "coverage": {
            "event_pixel_centres": int(inside_event.sum()),
            "event_joint_usable_pixels": int(event_joint.sum()),
            "event_joint_usable_fraction": event_fraction,
            "control_ring_pixel_centres": int(inside_ring.sum()),
            "control_ring_joint_usable_pixels": int(ring_joint.sum()),
            "control_ring_joint_usable_fraction": ring_fraction,
            "coverage_gate_passed": coverage_passed,
        },
        "dnbr": {
            "definition": "NBR_pre minus NBR_post, with NBR equal to B08 minus B12 divided by B08 plus B12",
            "event": event_summary,
            "control_ring": ring_summary,
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
            "sha256": sha256(INTERIM),
        },
        "interpretation": (
            "Passing this check would show stronger Sentinel-2 spectral change inside the GWIS "
            "event than in a nearby mapped-fire-free ring. It would not validate every boundary "
            "pixel, estimate severity, establish a fire cause, or make GWIS independent of FIRMS."
        ),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
