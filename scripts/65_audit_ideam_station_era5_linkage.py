"""Audit spatial and temporal compatibility between IDEAM gauges and ERA5 Land.

This script does not aggregate ERA5 Land and does not compare meteorological
values. It only documents candidate spatial keys and observed daily coverage.
"""

from __future__ import annotations

import csv
import json
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATION_CATALOG = ROOT / "data" / "interim" / "ideam_precipitation_station_catalog_linkage.csv"
PRECIP_AUDIT = ROOT / "results" / "exploratory" / "audit_ideam_precipitation_initial_municipal_download.json"
ERA5_GRID = ROOT / "data" / "interim" / "era5_land_grid_municipality_linkage.csv"
OUTPUT_CSV = ROOT / "data" / "interim" / "ideam_precipitation_station_era5_linkage.csv"
OUTPUT_JSON = ROOT / "results" / "exploratory" / "audit_ideam_precipitation_station_era5_linkage.json"


def normalize_municipality(value: str) -> str:
    value = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()
    value = unicodedata.normalize("NFKD", value)
    return "".join(char for char in value if not unicodedata.combining(char)).upper()


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_km = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * radius_km * math.asin(math.sqrt(a))


def load_precip_coverage() -> dict[str, dict]:
    audit = json.loads(PRECIP_AUDIT.read_text(encoding="utf-8-sig"))
    coverage: dict[str, dict] = {}
    for file_record in audit["files"]:
        for station in file_record["stations"]:
            coverage[station["station_code"]] = {
                "source_csv": file_record["file"],
                "series_first_date": station["first_date"],
                "series_last_date": station["last_date"],
                "august_2026_unique_dates": station["august_2026_unique_dates"],
                "august_2026_first_date": station["august_2026_first_date"],
                "august_2026_last_date": station["august_2026_last_date"],
                "august_2026_approval_levels": "|".join(station["august_2026_approval_levels"]),
            }
    return coverage


def main() -> None:
    if OUTPUT_CSV.exists() or OUTPUT_JSON.exists():
        raise FileExistsError("El resultado de enlace ya existe y no se sobrescribirá")

    coverage = load_precip_coverage()
    with STATION_CATALOG.open(encoding="utf-8-sig", newline="") as handle:
        stations = list(csv.DictReader(handle))
    with ERA5_GRID.open(encoding="utf-8-sig", newline="") as handle:
        grid = list(csv.DictReader(handle))

    parsed_grid = [
        {
            "grid_id": int(item["grid_id"]),
            "longitude": float(item["longitude"]),
            "latitude": float(item["latitude"]),
            "municipality": item["municipality"],
            "municipality_normalized": normalize_municipality(item["municipality"])
            if item["municipality"]
            else "",
        }
        for item in grid
    ]

    rows: list[dict[str, object]] = []
    for station in stations:
        code = station["station_code"]
        longitude = float(station["geometry_x_epsg4326"])
        latitude = float(station["geometry_y_epsg4326"])
        municipality = station["official_municipality"]
        municipality_normalized = normalize_municipality(municipality)

        distances = [
            (haversine_km(longitude, latitude, cell["longitude"], cell["latitude"]), cell)
            for cell in parsed_grid
        ]
        global_distance, global_cell = min(distances, key=lambda pair: pair[0])
        same_municipality = [
            pair for pair in distances if pair[1]["municipality_normalized"] == municipality_normalized
        ]
        municipal_distance, municipal_cell = min(same_municipality, key=lambda pair: pair[0])
        station_coverage = coverage[code]

        rows.append(
            {
                "station_code": code,
                "station_name": station["station_name"],
                "official_municipality": municipality,
                "category": station["category"],
                "technology": station["technology"],
                "status": station["status"],
                "altitude_m": station["altitude_m"],
                "station_longitude": longitude,
                "station_latitude": latitude,
                "nearest_global_grid_id": global_cell["grid_id"],
                "nearest_global_grid_longitude": global_cell["longitude"],
                "nearest_global_grid_latitude": global_cell["latitude"],
                "nearest_global_grid_municipality": global_cell["municipality"],
                "nearest_global_distance_km": round(global_distance, 3),
                "nearest_global_same_municipality": (
                    global_cell["municipality_normalized"] == municipality_normalized
                ),
                "nearest_same_municipality_grid_id": municipal_cell["grid_id"],
                "nearest_same_municipality_grid_longitude": municipal_cell["longitude"],
                "nearest_same_municipality_grid_latitude": municipal_cell["latitude"],
                "nearest_same_municipality_distance_km": round(municipal_distance, 3),
                **station_coverage,
                "august_2026_complete_31_dates": (
                    station_coverage["august_2026_unique_dates"] == 31
                ),
            }
        )

    rows.sort(key=lambda item: str(item["station_code"]))
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    complete_by_municipality = Counter(
        normalize_municipality(str(row["official_municipality"]))
        for row in rows
        if row["august_2026_complete_31_dates"]
    )
    report = {
        "station_catalog_linkage": str(STATION_CATALOG.relative_to(ROOT)).replace("\\", "/"),
        "precipitation_coverage_audit": str(PRECIP_AUDIT.relative_to(ROOT)).replace("\\", "/"),
        "era5_grid_linkage": str(ERA5_GRID.relative_to(ROOT)).replace("\\", "/"),
        "interim_output": str(OUTPUT_CSV.relative_to(ROOT)).replace("\\", "/"),
        "station_count": len(rows),
        "stations_with_nearest_grid_center": len(rows),
        "stations_with_same_municipality_grid_center_candidate": sum(
            row["nearest_same_municipality_grid_id"] is not None for row in rows
        ),
        "nearest_global_center_in_same_municipality": sum(
            row["nearest_global_same_municipality"] is True for row in rows
        ),
        "nearest_global_center_outside_same_municipality": sum(
            row["nearest_global_same_municipality"] is False for row in rows
        ),
        "nearest_global_distance_km_min": min(row["nearest_global_distance_km"] for row in rows),
        "nearest_global_distance_km_max": max(row["nearest_global_distance_km"] for row in rows),
        "nearest_global_distance_km_mean": round(
            sum(row["nearest_global_distance_km"] for row in rows) / len(rows), 3
        ),
        "stations_with_all_31_august_dates": sum(
            row["august_2026_complete_31_dates"] is True for row in rows
        ),
        "municipalities_with_at_least_one_complete_august_station": sorted(complete_by_municipality),
        "municipality_complete_station_counts": dict(sorted(complete_by_municipality.items())),
        "interpretation_limit": (
            "Nearest-center links document spatial compatibility only. They do not establish "
            "that ERA5 Land equals the gauge observation, that a cell represents a municipality, "
            "or that a gauge represents conditions at any fire location. No meteorological values "
            "were compared or aggregated. Missing station dates are not imputed."
        ),
    }
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    for row in rows:
        print(
            row["station_code"],
            row["official_municipality"],
            row["nearest_global_distance_km"],
            row["nearest_global_same_municipality"],
            row["august_2026_unique_dates"],
            sep=" | ",
        )


if __name__ == "__main__":
    main()
