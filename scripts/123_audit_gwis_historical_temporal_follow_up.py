"""Audit whether August-only GWIS requests change under a common follow-up window."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from collections import defaultdict
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
SEASONS = (2018, 2020, 2022, 2024, 2025)
ORIGINAL_RAW = ROOT / "data" / "raw" / f"gwis_historical_access_probe_{ACCESS_DATE}"
RAW_DIR = ROOT / "data" / "raw" / f"gwis_historical_follow_up_probe_{ACCESS_DATE}"
PARTIAL_DIR = ROOT / "data" / "raw" / f".gwis_historical_follow_up_probe_{ACCESS_DATE}.partial"
BOUNDARY_SOURCE = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
BASELINE_SUMMARY = ROOT / "data" / "interim" / f"gwis_historical_event_schema_summary_{ACCESS_DATE}_v2.csv"
CONFIG = ROOT / "configs" / "historical_cohort_comparability_gate_v1.json"
INTERIM = ROOT / "data" / "interim" / f"gwis_historical_temporal_follow_up_audit_{ACCESS_DATE}.csv"
OUTPUT = ROOT / "results" / "exploratory" / f"audit_gwis_historical_temporal_follow_up_{ACCESS_DATE}.json"
TILE_PIXELS = 1024
MAX_WORKERS = 8


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


def transform_coordinates(value: object, bbox: tuple[float, float, float, float], extent: int):
    west, south, east, north = bbox
    if isinstance(value, list) and len(value) >= 2 and isinstance(value[0], (int, float)) and isinstance(value[1], (int, float)):
        return [west + float(value[0]) / extent * (east - west), south + float(value[1]) / extent * (north - south)]
    if isinstance(value, list):
        return [transform_coordinates(item, bbox, extent) for item in value]
    raise TypeError(f"Unexpected tile coordinate type: {type(value)!r}")


def download_tile(season: int, tile: dict[str, object]) -> dict[str, object]:
    bbox = tile["bbox"]
    response = request(
        {
            "service": "WMS",
            "version": "1.1.1",
            "request": "GetMap",
            "layers": LAYER,
            "styles": "",
            "srs": "EPSG:4326",
            "bbox": ",".join(f"{float(value):.12f}" for value in bbox),
            "width": TILE_PIXELS,
            "height": TILE_PIXELS,
            "format": "application/vnd.mapbox-vector-tile",
            "transparent": "true",
            "time": f"{season}-08-01/{season}-09-15",
        }
    )
    content_type = response.headers.get("Content-Type", "")
    if "application/vnd.mapbox-vector-tile" not in content_type:
        raise RuntimeError(f"Unexpected content type for {season}, {tile['filename']}: {content_type}")
    decoded = mapbox_vector_tile.decode(response.content)
    feature_count = len(decoded.get(LAYER, {}).get("features", []))
    return {**tile, "content": response.content, "content_type": content_type, "feature_count": feature_count}


def recovered_tile(tile: dict[str, object], tile_path: Path) -> dict[str, object]:
    decoded = mapbox_vector_tile.decode(tile_path.read_bytes())
    return {
        **tile,
        "content_type": "recovered_from_partial_raw_tile",
        "feature_count": len(decoded.get(LAYER, {}).get("features", [])),
    }


def reconstruct_tolima_events(raw_tiles: list[dict[str, object]], season_dir: Path, tolima: object) -> dict[str, dict[str, object]]:
    geometry_parts: dict[str, list] = defaultdict(list)
    property_versions: dict[str, set[tuple[tuple[str, str], ...]]] = defaultdict(set)
    for tile in raw_tiles:
        tile_path = season_dir / str(tile["filename"])
        decoded = mapbox_vector_tile.decode(tile_path.read_bytes())
        layer = decoded.get(LAYER, {"extent": 4096, "features": []})
        extent = int(layer.get("extent", 4096))
        bbox = tuple(float(value) for value in tile["bbox"])
        for feature in layer.get("features", []):
            properties = feature.get("properties", {})
            event_id = str(properties.get("fire_id") or properties.get("id") or feature.get("id"))
            coordinates = transform_coordinates(feature["geometry"]["coordinates"], bbox, extent)
            geometry = shape({"type": feature["geometry"]["type"], "coordinates": coordinates})
            if not geometry.is_valid:
                geometry = geometry.buffer(0)
            geometry_parts[event_id].append(geometry)
            property_versions[event_id].add(tuple(sorted((key, str(value)) for key, value in properties.items())))
    retained: dict[str, dict[str, object]] = {}
    for event_id, pieces in geometry_parts.items():
        geometry = unary_union(pieces)
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty or not geometry.intersects(tolima):
            continue
        versions = property_versions[event_id]
        if len(versions) != 1:
            raise RuntimeError(f"Property conflict within follow-up event {event_id}")
        properties = dict(next(iter(versions)))
        retained[event_id] = {
            "initialdate": properties.get("initialdate"),
            "finaldate": properties.get("finaldate"),
            "area": properties.get("area"),
        }
    return retained


def main() -> None:
    for path in (RAW_DIR, OUTPUT, INTERIM):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {path}")
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    protocol = config["required_source_checks"]["gwis_temporal_follow_up"]["pre_execution_protocol"]
    original_index = json.loads((ORIGINAL_RAW / "download_index.json").read_text(encoding="utf-8"))
    with BOUNDARY_SOURCE.open(encoding="utf-8-sig") as handle:
        tolima = unary_union([shape(feature["geometry"]) for feature in json.load(handle)["features"]])
    baseline_rows = list(csv.DictReader(BASELINE_SUMMARY.open(encoding="utf-8")))
    baseline_by_season = {
        season: {row["fire_id"]: row for row in baseline_rows if int(row["season"]) == season}
        for season in SEASONS
    }
    capability_path = PARTIAL_DIR / "gwis_wms_getcapabilities_1.1.1.xml"
    if PARTIAL_DIR.exists():
        if not capability_path.exists():
            raise RuntimeError("Partial directory exists without the original capabilities response")
        capabilities = capability_path.read_bytes()
    else:
        PARTIAL_DIR.mkdir(parents=True)
        capabilities = request({"service": "WMS", "version": "1.1.1", "request": "GetCapabilities"}).content
        capability_path.write_bytes(capabilities)
    if f"<Name>{LAYER}</Name>" not in capabilities.decode("utf-8", errors="replace"):
        raise RuntimeError("GWIS capabilities do not expose the requested polygon layer")
    season_tiles: dict[int, list[dict[str, object]]] = {}
    for season in SEASONS:
        source_tiles = original_index["season_summaries"][str(season)]["tiles"]
        season_dir = PARTIAL_DIR / str(season) / "tiles"
        season_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[dict[str, object]] = []
        pending_tiles = [tile for tile in source_tiles if not (season_dir / str(tile["filename"])).exists()]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(download_tile, season, tile): tile for tile in pending_tiles}
            for future in as_completed(futures):
                item = future.result()
                content = item.pop("content")
                (season_dir / str(item["filename"])).write_bytes(content)
                downloaded.append(item)
        for tile in source_tiles:
            if tile not in pending_tiles:
                downloaded.append(recovered_tile(tile, season_dir / str(tile["filename"])))
        downloaded.sort(key=lambda item: (int(item["row"]), int(item["column"])))
        if len(downloaded) != len(source_tiles):
            raise RuntimeError(f"Incomplete tile download for {season}")
        season_tiles[season] = downloaded
    index = {
        "audit_date": ACCESS_DATE,
        "query_window": "August 1 through September 15 inclusive",
        "seasons": list(SEASONS),
        "layer": LAYER,
        "tile_source_index": str((ORIGINAL_RAW / "download_index.json").relative_to(ROOT)),
        "season_tiles": {str(season): items for season, items in season_tiles.items()},
        "capabilities_sha256": sha256_file(capability_path),
    }
    index_path = PARTIAL_DIR / "download_index.json"
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    followup_by_season = {
        season: reconstruct_tolima_events(season_tiles[season], PARTIAL_DIR / str(season) / "tiles", tolima)
        for season in SEASONS
    }
    rows: list[dict[str, object]] = []
    seasonal_summary: dict[str, object] = {}
    for season in SEASONS:
        baseline = baseline_by_season[season]
        followup = followup_by_season[season]
        missing = sorted(set(baseline) - set(followup))
        changed_initial = 0
        changed_final = 0
        final_after_sep15 = 0
        for event_id, baseline_row in sorted(baseline.items()):
            followup_row = followup.get(event_id)
            row = {
                "season": season,
                "fire_id": event_id,
                "present_in_follow_up": followup_row is not None,
                "baseline_initialdate": baseline_row["initialdate"],
                "follow_up_initialdate": followup_row["initialdate"] if followup_row else None,
                "baseline_finaldate": baseline_row["finaldate"],
                "follow_up_finaldate": followup_row["finaldate"] if followup_row else None,
                "initialdate_changed": (followup_row is not None and baseline_row["initialdate"] != followup_row["initialdate"]),
                "finaldate_changed": (followup_row is not None and baseline_row["finaldate"] != followup_row["finaldate"]),
                "follow_up_final_after_september_15": (followup_row is not None and str(followup_row["finaldate"])[:10] > f"{season}-09-15"),
            }
            changed_initial += int(bool(row["initialdate_changed"]))
            changed_final += int(bool(row["finaldate_changed"]))
            final_after_sep15 += int(bool(row["follow_up_final_after_september_15"]))
            rows.append(row)
        followup_after_august = sum(str(value["initialdate"])[:10] > f"{season}-08-31" for value in followup.values())
        seasonal_summary[str(season)] = {
            "baseline_august_event_count": len(baseline),
            "follow_up_intersecting_tolima_event_count": len(followup),
            "baseline_events_missing_from_follow_up": missing,
            "baseline_initialdate_changed_count": changed_initial,
            "baseline_finaldate_changed_count": changed_final,
            "baseline_follow_up_final_after_september_15_count": final_after_sep15,
            "follow_up_events_starting_after_august_count": followup_after_august,
        }
    if any(summary["baseline_events_missing_from_follow_up"] for summary in seasonal_summary.values()):
        raise RuntimeError("At least one August baseline event disappeared from the follow-up response")
    fieldnames = list(rows[0])
    with INTERIM.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    shutil.move(str(PARTIAL_DIR), str(RAW_DIR))
    output = {
        "audit_date": ACCESS_DATE,
        "inference_class": "descriptive request-window stability audit",
        "protocol": protocol,
        "raw_input_directory": str(RAW_DIR.relative_to(ROOT)),
        "baseline_summary": str(BASELINE_SUMMARY.relative_to(ROOT)),
        "baseline_summary_sha256": sha256_file(BASELINE_SUMMARY),
        "seasonal_summary": seasonal_summary,
        "acceptance_result": "pass",
        "event_comparison_csv": str(INTERIM.relative_to(ROOT)),
        "interpretation_limit": protocol["interpretation_limit"],
    }
    OUTPUT.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "summary": seasonal_summary, "acceptance_result": "pass"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
