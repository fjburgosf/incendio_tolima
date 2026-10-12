"""Screen Sentinel-2 SCL dates for the largest GWIS event without using NBR.

The image-selection rule is frozen before inspecting spectral change. A date
must have at least 80 percent usable pixels inside the GWIS polygon. Usable SCL
classes are vegetation, bare soil, water, and unclassified. Among qualifying
dates, select the closest pre-event date and the closest post-event date.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import tifffile
from shapely import contains_xy
from shapely.geometry import shape


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
EVENT_SOURCE = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_august_2026.geojson"
FIRE_ID = "15606221"
ACCESS_DATE = "2026-09-06"
RAW_DIR = ROOT / "data" / "raw" / f"sentinel2_gwis_event_{FIRE_ID}_scl_{ACCESS_DATE}"
OUTPUT = ROOT / "results" / "exploratory" / f"screen_sentinel2_scl_gwis_event_{FIRE_ID}.json"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
ODATA_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
CATALOG_START = date(2026, 7, 1)
CATALOG_END_EXCLUSIVE = date(2026, 9, 6)
USABLE_SCL = {4, 5, 6, 7}
OBSTRUCTED_SCL = {0, 1, 2, 3, 8, 9, 10, 11}
MIN_USABLE_FRACTION = 0.80
PIXEL_SIZE_METRES = 20.0
PADDING_DEGREES = 0.005


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


def attribute_value(product: dict, name: str):
    for attribute in product.get("Attributes", []):
        if attribute.get("Name") == name:
            return attribute.get("Value")
    return None


def catalogue_query(token: str, bbox: tuple[float, float, float, float]) -> tuple[dict, str]:
    west, south, east, north = bbox
    polygon_wkt = (
        f"POLYGON(({west} {south},{east} {south},{east} {north},"
        f"{west} {north},{west} {south}))"
    )
    filter_expression = (
        "Collection/Name eq 'SENTINEL-2' and "
        "Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and "
        "att/OData.CSC.StringAttribute/Value eq 'S2MSI2A') and "
        f"OData.CSC.Intersects(area=geography'SRID=4326;{polygon_wkt}') and "
        f"ContentDate/Start ge {CATALOG_START.isoformat()}T00:00:00.000Z and "
        f"ContentDate/Start lt {CATALOG_END_EXCLUSIVE.isoformat()}T00:00:00.000Z"
    )
    query = urlencode({"$filter": filter_expression, "$expand": "Attributes", "$top": "500"})
    request = Request(
        f"{ODATA_URL}?{query}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urlopen(request, timeout=120) as response:
        return json.load(response), request.full_url


def raster_dimensions(bbox: tuple[float, float, float, float]) -> tuple[int, int]:
    west, south, east, north = bbox
    latitude = (south + north) / 2
    width_metres = (east - west) * 111_320 * math.cos(math.radians(latitude))
    height_metres = (north - south) * 110_574
    return max(1, math.ceil(width_metres / PIXEL_SIZE_METRES)), max(
        1, math.ceil(height_metres / PIXEL_SIZE_METRES)
    )


def process_body(
    bbox: tuple[float, float, float, float], width: int, height: int, day: date
) -> dict:
    next_day = day + timedelta(days=1)
    return {
        "input": {
            "bounds": {
                "bbox": list(bbox),
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
            '//VERSION=3\nfunction setup() { return { input: ["SCL", "dataMask"], '
            'output: { bands: 2, sampleType: "UINT16" } }; }\n'
            "function evaluatePixel(s) { return [s.SCL, s.dataMask]; }"
        ),
    }


def download_scl(
    token: str,
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    day: date,
) -> tuple[bytes, dict, str]:
    body = process_body(bbox, width, height, day)
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
        raise RuntimeError(f"Unexpected Process API response for {day}: {content_type}")
    return payload, body, content_type


def main() -> None:
    for target in (RAW_DIR, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {target}")
    with EVENT_SOURCE.open(encoding="utf-8") as handle:
        collection = json.load(handle)
    event_feature = next(
        feature for feature in collection["features"] if feature["properties"]["fire_id"] == FIRE_ID
    )
    event_geometry = shape(event_feature["geometry"])
    properties = event_feature["properties"]
    initial_utc = datetime.strptime(
        properties["initial_datetime_as_exposed"], "%Y-%m-%d %H:%M:%S"
    )
    final_utc = datetime.strptime(
        properties["final_datetime_as_exposed"], "%Y-%m-%d %H:%M:%S"
    )
    west, south, east, north = event_geometry.bounds
    bbox = (
        west - PADDING_DEGREES,
        south - PADDING_DEGREES,
        east + PADDING_DEGREES,
        north + PADDING_DEGREES,
    )
    width, height = raster_dimensions(bbox)

    token = access_token(read_env(ENV_FILE))
    catalogue, catalogue_url = catalogue_query(token, bbox)
    RAW_DIR.mkdir(parents=True)
    catalogue_path = RAW_DIR / "cdse_odata_catalogue_response.json"
    catalogue_path.write_text(json.dumps(catalogue, ensure_ascii=False, indent=2), encoding="utf-8")
    products = []
    for product in catalogue.get("value", []):
        products.append(
            {
                "product_id": product.get("Id"),
                "product_name": product.get("Name"),
                "acquisition_start": product.get("ContentDate", {}).get("Start"),
                "cloud_cover_percent_product": attribute_value(product, "cloudCover"),
                "processing_baseline": attribute_value(product, "processingBaseline"),
                "s3_path": product.get("S3Path"),
            }
        )
    if not products:
        raise RuntimeError("The official CDSE catalogue returned no Sentinel-2 L2A products")

    product_dates = sorted(
        {
            datetime.fromisoformat(product["acquisition_start"].replace("Z", "+00:00")).date()
            for product in products
            if product["acquisition_start"]
        }
    )
    pre_dates = [day for day in product_dates if day < initial_utc.date()]
    post_dates = [day for day in product_dates if day > final_utc.date()]
    screening_dates = pre_dates + post_dates

    xs = bbox[0] + (np.arange(width) + 0.5) / width * (bbox[2] - bbox[0])
    ys = bbox[3] - (np.arange(height) + 0.5) / height * (bbox[3] - bbox[1])
    inside_event = contains_xy(event_geometry, xs[np.newaxis, :], ys[:, np.newaxis])
    inside_count = int(inside_event.sum())
    if inside_count == 0:
        raise RuntimeError("Raster grid has no pixel centres inside the event geometry")

    observations = []
    for day in screening_dates:
        payload, body, content_type = download_scl(token, bbox, width, height, day)
        tif_path = RAW_DIR / f"sentinel2_scl_event_{FIRE_ID}_{day.isoformat()}.tif"
        manifest_path = RAW_DIR / f"sentinel2_scl_event_{FIRE_ID}_{day.isoformat()}_manifest.json"
        tif_path.write_bytes(payload)
        image = tifffile.imread(io.BytesIO(payload))
        if image.ndim != 3 or image.shape[2] != 2:
            raise RuntimeError(f"Unexpected SCL raster shape for {day}: {image.shape}")
        scl = image[:, :, 0].astype(int)
        data_mask = image[:, :, 1] > 0
        counts = Counter(int(value) for value in scl[inside_event].ravel())
        usable = inside_event & data_mask & np.isin(scl, list(USABLE_SCL))
        obstructed = inside_event & np.isin(scl, list(OBSTRUCTED_SCL))
        observation = {
            "date": day.isoformat(),
            "period": "pre" if day < initial_utc.date() else "post",
            "shape": [int(value) for value in image.shape],
            "event_pixel_count": inside_count,
            "data_mask_pixel_count": int((inside_event & data_mask).sum()),
            "usable_pixel_count": int(usable.sum()),
            "obstructed_pixel_count": int(obstructed.sum()),
            "usable_fraction_of_event": float(usable.sum() / inside_count),
            "data_mask_fraction_of_event": float((inside_event & data_mask).sum() / inside_count),
            "scl_counts_inside_event": dict(sorted(counts.items())),
            "qualifies_at_predeclared_threshold": bool(usable.sum() / inside_count >= MIN_USABLE_FRACTION),
            "source_file": str(tif_path.relative_to(ROOT)),
            "sha256": sha256(tif_path),
        }
        observations.append(observation)
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
                    "purpose": "SCL-only screening under a criterion fixed before NBR inspection",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    qualified_pre = [
        observation
        for observation in observations
        if observation["period"] == "pre" and observation["qualifies_at_predeclared_threshold"]
    ]
    qualified_post = [
        observation
        for observation in observations
        if observation["period"] == "post" and observation["qualifies_at_predeclared_threshold"]
    ]
    selected_pre = max(qualified_pre, key=lambda item: item["date"], default=None)
    selected_post = min(qualified_post, key=lambda item: item["date"], default=None)
    result = {
        "audit_date": ACCESS_DATE,
        "purpose": "SCL-only date selection for a later optical validation, not burn classification",
        "fire_id": FIRE_ID,
        "selection_reason": "largest reported GWIS NRT event intersecting Tolima",
        "event_initial_datetime_as_exposed": properties["initial_datetime_as_exposed"],
        "event_final_datetime_as_exposed": properties["final_datetime_as_exposed"],
        "bbox_wgs84_with_padding": bbox,
        "nominal_pixel_size_metres": PIXEL_SIZE_METRES,
        "output_width": width,
        "output_height": height,
        "predeclared_selection_rule": {
            "usable_scl_classes": sorted(USABLE_SCL),
            "obstructed_scl_classes": sorted(OBSTRUCTED_SCL),
            "minimum_usable_fraction_inside_event": MIN_USABLE_FRACTION,
            "pre_selection": "latest qualifying acquisition before the event initial date",
            "post_selection": "earliest qualifying acquisition after the event final date",
            "fallback": "none; if either side has no qualifying date, optical validation remains blocked",
        },
        "catalogue": {
            "url_without_credentials": catalogue_url,
            "response_file": str(catalogue_path.relative_to(ROOT)),
            "response_sha256": sha256(catalogue_path),
            "product_count": len(products),
            "unique_acquisition_dates": [day.isoformat() for day in product_dates],
            "products": products,
        },
        "observations": sorted(observations, key=lambda item: item["date"]),
        "selected_pre_date": selected_pre["date"] if selected_pre else None,
        "selected_post_date": selected_post["date"] if selected_post else None,
        "selection_gate_passed": selected_pre is not None and selected_post is not None,
        "interpretation_limit": (
            "SCL screening tests local observability only. It does not identify a burn scar, "
            "validate the GWIS perimeter, estimate severity, or establish fire cause."
        ),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
