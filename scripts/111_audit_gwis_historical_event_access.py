"""Audit whether the public GWIS NRT WMS exposes historical Tolima events.

This is a source-access audit, not an analysis of fire-event differences.  It
uses the same layer, coordinate reference system, tiling logic, and vector-tile
format as the August 2026 cohort download.  Each requested season is retained
as raw Mapbox vector tiles, with a manifest that intentionally contains no
credentials.
"""

from __future__ import annotations

import hashlib
import json
import math
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import mapbox_vector_tile
import requests
from shapely.geometry import shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[1]
ACCESS_DATE = "2026-09-07"
WMS_URL = "https://maps.effis.emergency.copernicus.eu/gwis"
LAYER = "nrt.ba.poly"
BOUNDARY_SOURCE = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
RAW_DIR = ROOT / "data" / "raw" / f"gwis_historical_access_probe_{ACCESS_DATE}"
PARTIAL_DIR = ROOT / "data" / "raw" / f".gwis_historical_access_probe_{ACCESS_DATE}.partial"
OUTPUT = ROOT / "results" / "exploratory" / f"audit_gwis_historical_access_{ACCESS_DATE}.json"

# These are access probes spanning the documented WMS temporal archive.  They
# are not yet a chosen analytic comparison window.
SEASONS = (2018, 2020, 2022, 2024, 2025, 2026)
TILE_DEGREES = 0.25
QUERY_PADDING_DEGREES = 0.05
TILE_PIXELS = 1024
MAX_WORKERS = 4


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def request(params: dict[str, object]) -> requests.Response:
    response = requests.get(
        WMS_URL,
        params=params,
        timeout=(20, 120),
        headers={"User-Agent": "tolima-fire-data-audit/1.0"},
    )
    response.raise_for_status()
    return response


def build_tiles(bounds: tuple[float, float, float, float]) -> list[dict[str, object]]:
    west, south, east, north = bounds
    west -= QUERY_PADDING_DEGREES
    south -= QUERY_PADDING_DEGREES
    east += QUERY_PADDING_DEGREES
    north += QUERY_PADDING_DEGREES
    columns = math.ceil((east - west) / TILE_DEGREES)
    rows = math.ceil((north - south) / TILE_DEGREES)
    tiles: list[dict[str, object]] = []
    for row in range(rows):
        for column in range(columns):
            tile_west = west + column * TILE_DEGREES
            tile_south = south + row * TILE_DEGREES
            tile_east = min(tile_west + TILE_DEGREES, east)
            tile_north = min(tile_south + TILE_DEGREES, north)
            tiles.append(
                {
                    "row": row,
                    "column": column,
                    "bbox": (tile_west, tile_south, tile_east, tile_north),
                    "filename": f"r{row:02d}_c{column:02d}.mvt",
                }
            )
    return tiles


def download_tile(season: int, tile: dict[str, object]) -> dict[str, object]:
    bbox = tile["bbox"]
    time_range = f"{season}-08-01/{season}-08-31"
    params = {
        "service": "WMS",
        "version": "1.1.1",
        "request": "GetMap",
        "layers": LAYER,
        "styles": "",
        "srs": "EPSG:4326",
        "bbox": ",".join(f"{value:.12f}" for value in bbox),
        "width": TILE_PIXELS,
        "height": TILE_PIXELS,
        "format": "application/vnd.mapbox-vector-tile",
        "transparent": "true",
        "time": time_range,
    }
    response = request(params)
    content_type = response.headers.get("Content-Type", "")
    if "application/vnd.mapbox-vector-tile" not in content_type:
        raise RuntimeError(
            f"Unexpected response for season {season}, {tile['filename']}: {content_type}"
        )
    decoded = mapbox_vector_tile.decode(response.content)
    layer = decoded.get(LAYER, {"features": []})
    features = layer.get("features", [])
    return {
        **tile,
        "content": response.content,
        "content_type": content_type,
        "feature_count": len(features),
        "sha256": sha256_bytes(response.content),
        "event_properties": [feature.get("properties", {}) for feature in features],
    }


def read_tile(tile_path: Path, tile: dict[str, object]) -> dict[str, object]:
    """Recover manifest information from an already completed raw tile."""
    content = tile_path.read_bytes()
    decoded = mapbox_vector_tile.decode(content)
    layer = decoded.get(LAYER, {"features": []})
    features = layer.get("features", [])
    return {
        **tile,
        "content_type": "application/vnd.mapbox-vector-tile",
        "feature_count": len(features),
        "sha256": sha256_bytes(content),
        "event_properties": [feature.get("properties", {}) for feature in features],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seasons",
        nargs="+",
        type=int,
        choices=SEASONS,
        help="Download only named seasons, then retain the resumable partial directory.",
    )
    arguments = parser.parse_args()
    requested_seasons = tuple(arguments.seasons) if arguments.seasons else SEASONS

    for target in (RAW_DIR, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {target}")

    with BOUNDARY_SOURCE.open(encoding="utf-8-sig") as handle:
        boundary_geojson = json.load(handle)
    tolima_geometry = unary_union([shape(feature["geometry"]) for feature in boundary_geojson["features"]])
    tiles = build_tiles(tuple(float(value) for value in tolima_geometry.bounds))

    capability_path = PARTIAL_DIR / "gwis_wms_getcapabilities_1.1.1.xml"
    if PARTIAL_DIR.exists():
        if not capability_path.exists():
            raise RuntimeError("Partial directory lacks the original GWIS capabilities response")
        capabilities_content = capability_path.read_bytes()
    else:
        PARTIAL_DIR.mkdir(parents=True)
        capabilities_content = request(
            {"service": "WMS", "version": "1.1.1", "request": "GetCapabilities"}
        ).content
        capability_path.write_bytes(capabilities_content)
    capabilities_text = capabilities_content.decode("utf-8", errors="replace")
    layer_index = capabilities_text.find(f"<Name>{LAYER}</Name>")
    layer_excerpt = capabilities_text[layer_index : layer_index + 1300] if layer_index >= 0 else ""
    mechanism_checks = {
        "polygon_layer_in_capabilities": layer_index >= 0,
        "mvt_format_in_capabilities": "application/vnd.mapbox-vector-tile" in capabilities_text,
        "documented_temporal_extent_includes_2001": "2001-01-01/2099-12-31" in layer_excerpt,
        "time_dimension_declared": '<Dimension name="time"' in layer_excerpt,
    }
    if not all(mechanism_checks.values()):
        raise RuntimeError(f"GWIS capability checks failed: {mechanism_checks}")

    season_summaries: dict[str, object] = {}
    for season in requested_seasons:
        season_dir = PARTIAL_DIR / str(season) / "tiles"
        season_dir.mkdir(parents=True, exist_ok=True)
        downloaded_tiles: list[dict[str, object]] = []
        pending_tiles = [tile for tile in tiles if not (season_dir / str(tile["filename"])).exists()]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(download_tile, season, tile): tile for tile in pending_tiles}
            for future in as_completed(futures):
                downloaded = future.result()
                (season_dir / str(downloaded["filename"])).write_bytes(downloaded.pop("content"))
                downloaded_tiles.append(downloaded)
        for tile in tiles:
            if tile not in pending_tiles:
                downloaded_tiles.append(read_tile(season_dir / str(tile["filename"]), tile))
        downloaded_tiles.sort(key=lambda item: (int(item["row"]), int(item["column"])))
        occurrences = Counter()
        initial_dates: list[str] = []
        final_dates: list[str] = []
        property_names = Counter()
        for tile in downloaded_tiles:
            for properties in tile.pop("event_properties"):
                event_id = str(properties.get("fire_id") or properties.get("id") or "missing_id")
                occurrences[event_id] += 1
                property_names.update(properties.keys())
                if properties.get("initialdate") is not None:
                    initial_dates.append(str(properties["initialdate"]))
                if properties.get("finaldate") is not None:
                    final_dates.append(str(properties["finaldate"]))
        season_summaries[str(season)] = {
            "requested_time_range": f"{season}-08-01/{season}-08-31",
            "tile_count": len(downloaded_tiles),
            "tiles_with_features": sum(tile["feature_count"] > 0 for tile in downloaded_tiles),
            "tile_feature_occurrences": sum(occurrences.values()),
            "unique_event_identifiers": len(occurrences),
            "initialdate_min": min(initial_dates) if initial_dates else None,
            "initialdate_max": max(initial_dates) if initial_dates else None,
            "finaldate_min": min(final_dates) if final_dates else None,
            "finaldate_max": max(final_dates) if final_dates else None,
            "property_names": sorted(property_names),
            "tiles": [
                {
                    key: value
                    for key, value in tile.items()
                    if key not in {"bbox"}
                }
                | {"bbox": list(tile["bbox"])}
                for tile in downloaded_tiles
            ],
        }

    if arguments.seasons:
        progress = {
            "access_date": ACCESS_DATE,
            "requested_seasons": list(requested_seasons),
            "complete_tile_counts": {
                str(season): sum(
                    (PARTIAL_DIR / str(season) / "tiles" / str(tile["filename"])).exists()
                    for tile in tiles
                )
                for season in SEASONS
            },
            "expected_tiles_per_season": len(tiles),
        }
        progress_path = PARTIAL_DIR / "resume_progress.json"
        progress_path.write_text(json.dumps(progress, indent=2), encoding="utf-8")
        print(json.dumps(progress, indent=2))
        return

    incomplete = [
        season
        for season in SEASONS
        if any(not (PARTIAL_DIR / str(season) / "tiles" / str(tile["filename"])).exists() for tile in tiles)
    ]
    if incomplete:
        raise RuntimeError(f"Historical probe is incomplete for seasons: {incomplete}")

    raw_index = {
        "access_date": ACCESS_DATE,
        "wms_url": WMS_URL,
        "layer": LAYER,
        "tolima_bbox": [round(value, 12) for value in tolima_geometry.bounds],
        "tile_degrees": TILE_DEGREES,
        "tile_pixels": TILE_PIXELS,
        "query_padding_degrees": QUERY_PADDING_DEGREES,
        "seasons": list(SEASONS),
        "capabilities_file": "gwis_wms_getcapabilities_1.1.1.xml",
        "capabilities_sha256": sha256_bytes(capabilities_content),
        "mechanism_checks": mechanism_checks,
        "season_summaries": season_summaries,
    }
    (PARTIAL_DIR / "download_index.json").write_text(
        json.dumps(raw_index, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    PARTIAL_DIR.rename(RAW_DIR)

    audit = {
        "audit_date": ACCESS_DATE,
        "inference_class": "descriptive source-access audit",
        "source": {
            "wms_url": WMS_URL,
            "layer": LAYER,
            "raw_directory": str(RAW_DIR.relative_to(ROOT)),
            "raw_download_index_sha256": sha256_file(RAW_DIR / "download_index.json"),
        },
        "mechanism_checks": mechanism_checks,
        "season_summaries": {
            season: {
                key: value
                for key, value in summary.items()
                if key != "tiles"
            }
            for season, summary in season_summaries.items()
        },
        "interpretation_rule": (
            "A nonzero, date-labelled response establishes that the public WMS returned "
            "historical event estimates for that requested period. It does not establish "
            "event completeness, stable processing across years, independent validation, "
            "or suitability as an analytic cohort without a later comparability audit."
        ),
    }
    OUTPUT.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(audit, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
