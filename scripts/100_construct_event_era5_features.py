"""Construct frozen anchor based ERA5 Land features for every GWIS event."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "analysis_plan_v1.json"
AMENDMENT = ROOT / "configs" / "analysis_plan_amendment_2026-09-06_mapbiomas.json"
MASTER = ROOT / "data" / "interim" / "gwis_event_master_v1.csv"
GRID = ROOT / "data" / "interim" / "era5_land_hourly_grid_v1.npz"
WEIGHTS = ROOT / "data" / "interim" / "gwis_event_era5_anchor_weights_v1.csv"
OUTPUT = ROOT / "data" / "interim" / "gwis_event_era5_features_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_gwis_event_era5_features_v1.json"
LOCAL_OFFSET = timezone(timedelta(hours=-5))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_local(value: str) -> datetime:
    return datetime.fromisoformat(value)


def find_bracketing(values: np.ndarray, target: float) -> tuple[float, float]:
    unique = np.unique(values)
    lower = unique[unique <= target]
    upper = unique[unique >= target]
    if lower.size == 0 or upper.size == 0:
        raise ValueError(f"Anchor outside ERA5 grid: {target}")
    return float(lower[-1]), float(upper[0])


def grid_index_mapping(latitudes: np.ndarray, longitudes: np.ndarray) -> dict[tuple[float, float], int]:
    mapping: dict[tuple[float, float], int] = {}
    for index, pair in enumerate(zip(latitudes, longitudes, strict=True)):
        key = (float(pair[0]), float(pair[1]))
        if key in mapping:
            raise RuntimeError(f"Repeated ERA5 grid coordinate: {key}")
        mapping[key] = index
    expected = len(np.unique(latitudes)) * len(np.unique(longitudes))
    if len(mapping) != expected:
        raise RuntimeError("ERA5 grid is not a complete regular latitude longitude lattice")
    return mapping


def bilinear_weights(latitudes: np.ndarray, longitudes: np.ndarray, longitude: float, latitude: float) -> list[dict]:
    index_map = grid_index_mapping(latitudes, longitudes)
    lat_low, lat_high = find_bracketing(latitudes, latitude)
    lon_low, lon_high = find_bracketing(longitudes, longitude)
    lat_fraction = 0.0 if lat_high == lat_low else (latitude - lat_low) / (lat_high - lat_low)
    lon_fraction = 0.0 if lon_high == lon_low else (longitude - lon_low) / (lon_high - lon_low)
    raw = [
        (lat_low, lon_low, (1 - lat_fraction) * (1 - lon_fraction)),
        (lat_low, lon_high, (1 - lat_fraction) * lon_fraction),
        (lat_high, lon_low, lat_fraction * (1 - lon_fraction)),
        (lat_high, lon_high, lat_fraction * lon_fraction),
    ]
    combined: dict[tuple[float, float], float] = {}
    for grid_lat, grid_lon, weight in raw:
        combined[(grid_lat, grid_lon)] = combined.get((grid_lat, grid_lon), 0.0) + weight
    weights = [{"grid_point_index": index_map[key], "grid_latitude": key[0], "grid_longitude": key[1], "weight": value}
               for key, value in sorted(combined.items())]
    if any(weight["weight"] < -1e-12 or weight["weight"] > 1 + 1e-12 for weight in weights):
        raise RuntimeError("Bilinear weight outside [0,1]")
    if not math.isclose(sum(weight["weight"] for weight in weights), 1.0, abs_tol=1e-12):
        raise RuntimeError("Bilinear weights do not sum to one")
    return weights


def interpolate(field: np.ndarray, weights: list[dict], indexes: list[int]) -> np.ndarray:
    point_indexes = np.asarray([weight["grid_point_index"] for weight in weights], dtype=int)
    coefficients = np.asarray([weight["weight"] for weight in weights], dtype=float)
    values = field[np.asarray(indexes, dtype=int)][:, point_indexes]
    return np.sum(values * coefficients[np.newaxis, :], axis=1)


def consecutive_dry_days(values_mm: list[float], threshold_mm: float) -> int:
    current = longest = 0
    for value in values_mm:
        if value < threshold_mm:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def required_window_indexes(times: list[datetime], initial: datetime) -> list[int]:
    end = initial + timedelta(hours=24)
    indexes = [index for index, when in enumerate(times) if initial < when <= end]
    if len(indexes) != 24:
        raise RuntimeError(f"Expected 24 primary hourly validity times after {initial.isoformat()}, found {len(indexes)}")
    return indexes


def daily_precipitation_indexes(times: list[datetime], local_date) -> list[int]:
    start_local = datetime.combine(local_date, datetime.min.time(), tzinfo=LOCAL_OFFSET)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    # An accumulation at validity t represents its immediately preceding hourly interval.
    return [index for index, when in enumerate(times) if start_utc < when <= end_utc]


def main() -> None:
    for target in (WEIGHTS, OUTPUT, AUDIT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    amendment = json.loads(AMENDMENT.read_text(encoding="utf-8"))
    if plan["version"] != "1.0" or amendment["amendment_version"] != "1.1":
        raise RuntimeError("Unexpected analysis plan version")
    with MASTER.open(encoding="utf-8-sig", newline="") as handle:
        events = list(csv.DictReader(handle))
    if len(events) != 62 or len({row["fire_id"] for row in events}) != 62:
        raise RuntimeError("Unexpected event-master cardinality")
    grid = np.load(GRID, allow_pickle=False)
    latitudes = grid["latitudes"].astype(float)
    longitudes = grid["longitudes"].astype(float)
    times = [parse_utc(str(value)) for value in grid["validity_utc"]]
    if any(later <= earlier for earlier, later in zip(times, times[1:])):
        raise RuntimeError("ERA5 validity times must be strictly increasing")
    fields = {name: grid[name].astype(float) for name in ("2t", "2d", "10u", "10v", "tp")}
    if any(value.shape != (len(times), len(latitudes)) for value in fields.values()):
        raise RuntimeError("Unexpected ERA5 array shape")
    weight_rows, feature_rows = [], []
    invalid_vpd_events = []
    for event in events:
        fire_id = event["fire_id"]
        anchor_lon, anchor_lat = float(event["anchor_longitude"]), float(event["anchor_latitude"])
        weights = bilinear_weights(latitudes, longitudes, anchor_lon, anchor_lat)
        for rank, weight in enumerate(weights, start=1):
            weight_rows.append({"fire_id": fire_id, "anchor_longitude": anchor_lon, "anchor_latitude": anchor_lat, "corner_rank": rank, **weight})
        initial = parse_utc(event["gwis_initial_datetime_utc"])
        primary_indexes = required_window_indexes(times, initial)
        primary_t_c = interpolate(fields["2t"], weights, primary_indexes) - 273.15
        primary_td_c = interpolate(fields["2d"], weights, primary_indexes) - 273.15
        primary_u = interpolate(fields["10u"], weights, primary_indexes)
        primary_v = interpolate(fields["10v"], weights, primary_indexes)
        primary_tp_mm = interpolate(fields["tp"], weights, primary_indexes) * 1000.0
        es_t = 0.6108 * np.exp((17.27 * primary_t_c) / (primary_t_c + 237.3))
        es_td = 0.6108 * np.exp((17.27 * primary_td_c) / (primary_td_c + 237.3))
        vpd = es_t - es_td
        materially_negative = vpd < plan["derivations"]["vpd_negative_tolerance_kpa"]
        if materially_negative.any():
            invalid_vpd_events.append({"fire_id": fire_id, "minimum_vpd_kpa": float(vpd.min())})
        vpd[(vpd < 0) & ~materially_negative] = 0.0
        wind = np.sqrt(primary_u ** 2 + primary_v ** 2)
        initial_local = parse_local(event["gwis_initial_datetime_colombia"])
        local_start_date = initial_local.date()
        antecedent_7_dates = [local_start_date - timedelta(days=value) for value in range(7, 0, -1)]
        antecedent_30_dates = [local_start_date - timedelta(days=value) for value in range(30, 0, -1)]
        daily_7 = []
        daily_30 = []
        for day in antecedent_7_dates:
            indexes = daily_precipitation_indexes(times, day)
            if len(indexes) != 24:
                raise RuntimeError(f"Incomplete antecedent seven day rainfall window for {fire_id} on {day.isoformat()}")
            daily_7.append(float(interpolate(fields["tp"], weights, indexes).sum() * 1000.0))
        for day in antecedent_30_dates:
            indexes = daily_precipitation_indexes(times, day)
            if len(indexes) != 24:
                raise RuntimeError(f"Incomplete antecedent thirty day rainfall window for {fire_id} on {day.isoformat()}")
            daily_30.append(float(interpolate(fields["tp"], weights, indexes).sum() * 1000.0))
        feature_rows.append({
            "fire_id": fire_id,
            "gwis_initial_datetime_utc": event["gwis_initial_datetime_utc"],
            "gwis_initial_datetime_colombia": event["gwis_initial_datetime_colombia"],
            "weather_window_first_validity_utc": times[primary_indexes[0]].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "weather_window_last_validity_utc": times[primary_indexes[-1]].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "weather_window_hour_count": len(primary_indexes),
            "vpd_mean_first_24h_kpa": float(vpd.mean()),
            "vpd_p95_first_24h_kpa": float(np.quantile(vpd, 0.95)),
            "wind_mean_first_24h_ms": float(wind.mean()),
            "wind_p95_first_24h_ms": float(np.quantile(wind, 0.95)),
            "temperature_mean_first_24h_c": float(primary_t_c.mean()),
            "temperature_max_first_24h_c": float(primary_t_c.max()),
            "precipitation_first_24h_mm": float(primary_tp_mm.sum()),
            "precipitation_previous_7_complete_local_days_mm": float(sum(daily_7)),
            "maximum_consecutive_dry_days_previous_30_complete_local_days": consecutive_dry_days(daily_30, plan["derivations"]["dry_day_threshold_mm"]),
            "primary_weather_complete": True,
            "exploratory_precipitation_complete": True,
            "vpd_materially_negative_flag": bool(materially_negative.any()),
        })
    if invalid_vpd_events:
        raise RuntimeError(f"VPD quality gate failed: {invalid_vpd_events}")
    if len(feature_rows) != 62 or not all(row["primary_weather_complete"] for row in feature_rows):
        raise RuntimeError("Primary weather completeness gate failed")
    weight_rows.sort(key=lambda row: (row["fire_id"], row["corner_rank"]))
    feature_rows.sort(key=lambda row: row["fire_id"])
    with WEIGHTS.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(weight_rows[0]))
        writer.writeheader()
        writer.writerows(weight_rows)
    with OUTPUT.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(feature_rows[0]))
        writer.writeheader()
        writer.writerows(feature_rows)
    audit = {
        "plan": {"file": str(PLAN.relative_to(ROOT)), "sha256": sha256(PLAN), "version": plan["version"]},
        "amendment": {"file": str(AMENDMENT.relative_to(ROOT)), "sha256": sha256(AMENDMENT), "version": amendment["amendment_version"]},
        "event_master": {"file": str(MASTER.relative_to(ROOT)), "sha256": sha256(MASTER), "event_count": len(events)},
        "era5_grid": {"file": str(GRID.relative_to(ROOT)), "sha256": sha256(GRID), "hour_count": len(times), "grid_point_count": len(latitudes)},
        "weather_sampling_rule": "bilinear interpolation at frozen thermal anchor, never an average over the final GWIS polygon",
        "weights": {"file": str(WEIGHTS.relative_to(ROOT)), "sha256": sha256(WEIGHTS), "row_count": len(weight_rows), "weight_sum_minimum": min(sum(float(item["weight"]) for item in weight_rows if item["fire_id"] == fire_id) for fire_id in {row["fire_id"] for row in weight_rows}), "weight_sum_maximum": max(sum(float(item["weight"]) for item in weight_rows if item["fire_id"] == fire_id) for fire_id in {row["fire_id"] for row in weight_rows})},
        "feature_output": {"file": str(OUTPUT.relative_to(ROOT)), "sha256": sha256(OUTPUT), "event_count": len(feature_rows)},
        "primary_weather_complete_event_count": sum(row["primary_weather_complete"] for row in feature_rows),
        "exploratory_precipitation_complete_event_count": sum(row["exploratory_precipitation_complete"] for row in feature_rows),
        "materially_negative_vpd_event_count": sum(row["vpd_materially_negative_flag"] for row in feature_rows),
        "precipitation_interval_rule": "For each local calendar date, total precipitation uses the 24 hourly accumulated values ending strictly after local midnight and not later than the following local midnight.",
        "interpretation_limit": "ERA5 Land values are reanalysis estimates sampled at a thermal detection derived anchor. They do not measure meteorology at an ignition site and support only the exploratory associational design specified before outcomes were estimated.",
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "event_count": len(feature_rows),
        "primary_weather_complete": audit["primary_weather_complete_event_count"],
        "precipitation_complete": audit["exploratory_precipitation_complete_event_count"],
        "materially_negative_vpd_events": audit["materially_negative_vpd_event_count"],
        "weight_rows": len(weight_rows),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
