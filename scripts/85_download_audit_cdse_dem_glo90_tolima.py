"""Download and audit Copernicus DEM GLO 90 across Tolima.

The acceptance rule is fixed before download. Every one of the 47 municipal
polygons and every one of the 62 GWIS event polygons must contain at least one
valid DEM pixel centre. Elevation is audited as a static covariate only. No
slope, aspect, fire effect, or causal interpretation is derived.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import tifffile
from shapely import contains_xy
from shapely.geometry import shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
BOUNDARY_SOURCE = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
EVENT_SOURCE = ROOT / "data" / "interim" / "gwis_nrt_burned_area_events_tolima_august_2026.geojson"
RAW_DIR = ROOT / "data" / "raw" / "copernicus_dem_glo90_tolima_2026-09-06"
MUNICIPAL_CSV = ROOT / "data" / "interim" / "copernicus_dem_glo90_tolima_municipality_summary.csv"
EVENT_CSV = ROOT / "data" / "interim" / "copernicus_dem_glo90_gwis_event_summary.csv"
OUTPUT = ROOT / "results" / "exploratory" / "audit_cdse_dem_glo90_tolima_linkage.json"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
TARGET_PIXEL_METRES = 90.0


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def access_token(values: dict[str, str]) -> str:
    form = urlencode({
        "grant_type": "client_credentials",
        "client_id": values["CDSE_CLIENT_ID"],
        "client_secret": values["CDSE_CLIENT_SECRET"],
    }).encode("utf-8")
    request = Request(TOKEN_URL, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urlopen(request, timeout=60) as response:
        return json.load(response)["access_token"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def dimensions(bbox: list[float]) -> tuple[int, int, float, float]:
    west, south, east, north = bbox
    latitude = (south + north) / 2
    width_metres = (east - west) * 111_320 * math.cos(math.radians(latitude))
    height_metres = (north - south) * 110_574
    width = math.ceil(width_metres / TARGET_PIXEL_METRES)
    height = math.ceil(height_metres / TARGET_PIXEL_METRES)
    return width, height, width_metres / width, height_metres / height


def process_body(bbox: list[float], width: int, height: int) -> dict:
    return {
        "input": {
            "bounds": {"bbox": bbox, "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"}},
            "data": [{"type": "dem", "dataFilter": {"demInstance": "COPERNICUS_90"}}],
        },
        "output": {
            "width": width, "height": height,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": (
            '//VERSION=3\nfunction setup() { return { input: ["DEM", "dataMask"], '
            'output: { bands: 2, sampleType: "FLOAT32" } }; }\n'
            'function evaluatePixel(s) { return [s.DEM, s.dataMask]; }'
        ),
    }


def download(token: str, body: dict) -> tuple[bytes, str]:
    request = Request(PROCESS_URL, data=json.dumps(body).encode("utf-8"),
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=240) as response:
        payload = response.read()
        content_type = response.headers.get_content_type()
    if content_type not in {"image/tiff", "image/tiff; application=geotiff"}:
        raise RuntimeError(f"Unexpected content type {content_type}")
    return payload, content_type


def summarize(values: np.ndarray) -> dict:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return {"valid_pixel_centres": 0, "minimum_m": None, "q25_m": None, "median_m": None,
                "mean_m": None, "q75_m": None, "maximum_m": None}
    return {
        "valid_pixel_centres": int(finite.size), "minimum_m": float(np.min(finite)),
        "q25_m": float(np.quantile(finite, 0.25)), "median_m": float(np.median(finite)),
        "mean_m": float(np.mean(finite)), "q75_m": float(np.quantile(finite, 0.75)),
        "maximum_m": float(np.max(finite)),
    }


def values_for_geometry(geometry, tiles: list[dict]) -> np.ndarray:
    values = []
    for tile in tiles:
        west, south, east, north = tile["bbox"]
        if geometry.bounds[2] < west or geometry.bounds[0] > east or geometry.bounds[3] < south or geometry.bounds[1] > north:
            continue
        inside = contains_xy(geometry, tile["xs"][np.newaxis, :], tile["ys"][:, np.newaxis])
        selected = tile["dem"][inside & tile["valid"]]
        if selected.size:
            values.append(selected)
    return np.concatenate(values) if values else np.asarray([], dtype=np.float32)


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    for target in (RAW_DIR, MUNICIPAL_CSV, EVENT_CSV, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    boundaries = json.loads(BOUNDARY_SOURCE.read_text(encoding="utf-8-sig"))
    municipalities = [(feature["properties"]["mun_nombre"], shape(feature["geometry"])) for feature in boundaries["features"]]
    tolima = unary_union([geometry for _, geometry in municipalities])
    west, south, east, north = tolima.bounds
    split_latitude = (south + north) / 2
    bboxes = [[west, south, east, split_latitude], [west, split_latitude, east, north]]
    token = access_token(read_env(ENV_FILE))
    RAW_DIR.mkdir(parents=True)
    tiles, source_records = [], []
    for index, bbox in enumerate(bboxes, start=1):
        width, height, x_metres, y_metres = dimensions(bbox)
        body = process_body(bbox, width, height)
        payload, content_type = download(token, body)
        tif_path = RAW_DIR / f"copernicus_dem_glo90_tolima_tile_{index}.tif"
        manifest_path = RAW_DIR / f"copernicus_dem_glo90_tolima_tile_{index}_manifest.json"
        tif_path.write_bytes(payload)
        image = tifffile.imread(tif_path)
        if image.shape != (height, width, 2):
            raise RuntimeError(f"Unexpected tile shape {image.shape} for expected {(height, width, 2)}")
        xs = bbox[0] + (np.arange(width) + 0.5) / width * (bbox[2] - bbox[0])
        ys = bbox[3] - (np.arange(height) + 0.5) / height * (bbox[3] - bbox[1])
        tile = {"bbox": bbox, "xs": xs, "ys": ys, "dem": image[:, :, 0].astype(np.float32), "valid": image[:, :, 1] > 0}
        tiles.append(tile)
        manifest = {
            "source": "Copernicus Data Space Process API", "collection": "dem",
            "dem_instance": "COPERNICUS_90", "request": body, "file": tif_path.name,
            "bytes": len(payload), "sha256": sha256(tif_path), "content_type": content_type,
            "requested_target_pixel_metres": TARGET_PIXEL_METRES,
            "approximate_output_pixel_size_metres": [x_metres, y_metres],
            "purpose": "Static elevation source and spatial-linkage audit, not fire measurement",
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        source_records.append({"tile": index, "bbox": bbox, "shape": [height, width],
                               "approximate_pixel_size_metres": [x_metres, y_metres],
                               "file": str(tif_path.relative_to(ROOT)), "sha256": sha256(tif_path)})

    municipal_rows = []
    for name, geometry in municipalities:
        summary = summarize(values_for_geometry(geometry, tiles))
        municipal_rows.append({"municipality": name, **summary})
    municipal_rows.sort(key=lambda row: row["municipality"])
    write_csv(MUNICIPAL_CSV, municipal_rows)

    events = json.loads(EVENT_SOURCE.read_text(encoding="utf-8"))
    event_rows = []
    for feature in events["features"]:
        summary = summarize(values_for_geometry(shape(feature["geometry"]).intersection(tolima), tiles))
        event_rows.append({"fire_id": feature["properties"]["fire_id"], **summary})
    event_rows.sort(key=lambda row: row["fire_id"])
    write_csv(EVENT_CSV, event_rows)

    tolima_values = values_for_geometry(tolima, tiles)
    output = {
        "audit_date": "2026-09-06", "inference_class": "source and geographic linkage audit",
        "source": "Copernicus DEM GLO 90 through Copernicus Data Space Process API",
        "source_records": source_records,
        "acceptance_rule_fixed_before_download": {
            "all_47_municipalities_have_at_least_one_valid_pixel_centre": True,
            "all_62_gwis_events_have_at_least_one_valid_pixel_centre": True,
        },
        "tolima_valid_pixel_summary": summarize(tolima_values),
        "municipality_linkage": {
            "expected_count": 47, "observed_count": len(municipal_rows),
            "with_valid_pixels": sum(row["valid_pixel_centres"] > 0 for row in municipal_rows),
            "gate_passed": len(municipal_rows) == 47 and all(row["valid_pixel_centres"] > 0 for row in municipal_rows),
            "file": str(MUNICIPAL_CSV.relative_to(ROOT)), "sha256": sha256(MUNICIPAL_CSV),
        },
        "gwis_event_linkage": {
            "expected_count": 62, "observed_count": len(event_rows),
            "with_valid_pixels": sum(row["valid_pixel_centres"] > 0 for row in event_rows),
            "gate_passed": len(event_rows) == 62 and all(row["valid_pixel_centres"] > 0 for row in event_rows),
            "file": str(EVENT_CSV.relative_to(ROOT)), "sha256": sha256(EVENT_CSV),
        },
        "quality_controls": {
            "finite_values_inside_tolima": int(np.isfinite(tolima_values).sum()),
            "nonfinite_values_inside_tolima": int((~np.isfinite(tolima_values)).sum()),
            "tile_split_has_no_overlap": True,
        },
        "interpretation_limit": "GLO 90 is a digital surface model sampled on an API-defined grid near 90 metres. Summaries verify variable availability and geographic linkage. They are not ground-station elevations, slope, aspect, fire effects, or causal estimates.",
    }
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "tolima_valid_pixels": output["tolima_valid_pixel_summary"]["valid_pixel_centres"],
        "municipality_gate_passed": output["municipality_linkage"]["gate_passed"],
        "event_gate_passed": output["gwis_event_linkage"]["gate_passed"],
        "tolima_minimum_m": output["tolima_valid_pixel_summary"]["minimum_m"],
        "tolima_maximum_m": output["tolima_valid_pixel_summary"]["maximum_m"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
