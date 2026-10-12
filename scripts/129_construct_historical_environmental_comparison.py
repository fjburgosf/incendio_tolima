"""Construct audited August ERA5 Land environmental summaries for eligible seasons."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import eccodes
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "configs" / "historical_environmental_comparison_plan_v1.json"
GRID_LINK = ROOT / "data" / "interim" / "era5_land_grid_municipality_linkage.csv"
CURRENT_2026 = ROOT / "data" / "interim" / "era5_land_hourly_grid_v1.npz"
HISTORICAL_MANIFEST = ROOT / "data" / "raw" / "era5_land" / "era5_land_historical_event_window_source_2026-09-07_manifest_v2.json"
GWIS_HISTORY = ROOT / "data" / "interim" / "gwis_historical_event_schema_summary_2026-09-07_v2.csv"
DAILY_OUT = ROOT / "data" / "processed" / "historical_environmental_daily_august_v1.csv"
SEASON_OUT = ROOT / "data" / "processed" / "historical_environmental_season_summary_v1.csv"
AUDIT_OUT = ROOT / "results" / "primary" / "audit_historical_environmental_comparison_v1.json"
VARIABLES = ("2t", "2d", "10u", "10v")
EXPECTED_UNITS = {"2t": "K", "2d": "K", "10u": "m s**-1", "10v": "m s**-1"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def key_pairs(latitudes: np.ndarray, longitudes: np.ndarray) -> list[tuple[float, float]]:
    return [(round(float(lat), 6), round(float(lon), 6)) for lat, lon in zip(latitudes, longitudes)]


def expected_times(year: int) -> list[datetime]:
    start = datetime(year, 8, 1)
    return [start + timedelta(hours=index) for index in range(31 * 24)]


def vpd_kpa(air_temperature_k: np.ndarray, dewpoint_k: np.ndarray) -> np.ndarray:
    air_c = air_temperature_k - 273.15
    dew_c = dewpoint_k - 273.15
    saturation = 0.6108 * np.exp((17.27 * air_c) / (air_c + 237.3))
    actual = 0.6108 * np.exp((17.27 * dew_c) / (dew_c + 237.3))
    return saturation - actual


def load_grid_rule() -> tuple[list[tuple[float, float]], np.ndarray]:
    with GRID_LINK.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    inside = [row for row in rows if row["municipality_match_count"] == "1"]
    keys = [(round(float(row["latitude"]), 6), round(float(row["longitude"]), 6)) for row in inside]
    if len(rows) != 989 or len(inside) != 198 or len(set(keys)) != len(keys):
        raise RuntimeError("The frozen Tolima grid-centre rule no longer has 989 total and 198 unique retained points")
    return keys, np.cos(np.deg2rad(np.asarray([latitude for latitude, _ in keys], dtype=np.float64)))


def apply_grid_rule(
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    retained_keys: list[tuple[float, float]],
) -> np.ndarray:
    keys = key_pairs(latitudes, longitudes)
    if len(keys) != 989 or len(set(keys)) != 989:
        raise RuntimeError("ERA5 grid is not the expected 989 unique centres")
    index_by_key = {key: index for index, key in enumerate(keys)}
    if set(index_by_key) != set(key_pairs(np.asarray(latitudes), np.asarray(longitudes))):
        raise RuntimeError("ERA5 coordinate index construction failed")
    missing = [key for key in retained_keys if key not in index_by_key]
    if missing:
        raise RuntimeError(f"Retained Tolima centres are absent from source grid, first missing {missing[0]}")
    return np.asarray([index_by_key[key] for key in retained_keys], dtype=int)


def weighted_hourly(values: np.ndarray, weights: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return np.average(values, axis=1, weights=weights), np.mean(values, axis=1)


def read_historical_august(year: int, path: Path) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray], dict[str, object]]:
    records: dict[str, dict[datetime, np.ndarray]] = defaultdict(dict)
    reference_latitudes: np.ndarray | None = None
    reference_longitudes: np.ndarray | None = None
    units: dict[str, set[str]] = defaultdict(set)
    duplicate_records: list[str] = []
    with path.open("rb") as handle:
        while message := eccodes.codes_grib_new_from_file(handle):
            try:
                short_name = str(eccodes.codes_get(message, "shortName"))
                if short_name not in VARIABLES:
                    raise RuntimeError(f"Unexpected variable {short_name} in {path.name}")
                valid_date = str(eccodes.codes_get(message, "validityDate"))
                valid_time = int(eccodes.codes_get(message, "validityTime"))
                valid = datetime.strptime(f"{valid_date}{valid_time:04d}", "%Y%m%d%H%M")
                if valid in records[short_name]:
                    duplicate_records.append(f"{short_name} {valid.isoformat()}")
                    continue
                values = np.asarray(eccodes.codes_get_array(message, "values"), dtype=np.float64)
                latitudes = np.asarray(eccodes.codes_get_array(message, "latitudes"), dtype=np.float64)
                longitudes = np.asarray(eccodes.codes_get_array(message, "longitudes"), dtype=np.float64)
                if reference_latitudes is None:
                    reference_latitudes, reference_longitudes = latitudes, longitudes
                elif not (np.array_equal(latitudes, reference_latitudes) and np.array_equal(longitudes, reference_longitudes)):
                    raise RuntimeError(f"Inconsistent ERA5 grid in {path.name}")
                records[short_name][valid] = values
                units[short_name].add(str(eccodes.codes_get(message, "units")))
            finally:
                eccodes.codes_release(message)
    if reference_latitudes is None or reference_longitudes is None:
        raise RuntimeError(f"No GRIB records read from {path}")
    times = expected_times(year)
    fields = {}
    missing_counts = {}
    for short_name in VARIABLES:
        missing = sorted(set(times) - set(records[short_name]))
        unexpected = sorted(set(records[short_name]) - set(times))
        if missing or unexpected or units[short_name] != {EXPECTED_UNITS[short_name]}:
            raise RuntimeError(f"Historical coverage gate failed for {year} {short_name}")
        fields[short_name] = np.stack([records[short_name][time] for time in times], axis=0)
        missing_counts[short_name] = len(missing)
    if duplicate_records:
        raise RuntimeError(f"Historical duplicate time records, first {duplicate_records[0]}")
    return reference_latitudes, reference_longitudes, fields, {"missing_hours": missing_counts, "sha256": sha256(path)}


def load_2026_august() -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray], dict[str, object]]:
    archive = np.load(CURRENT_2026)
    times = [datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ") for value in archive["validity_utc"]]
    required = expected_times(2026)
    index = {time: position for position, time in enumerate(times)}
    missing = [time for time in required if time not in index]
    if missing:
        raise RuntimeError(f"2026 August coverage is incomplete, first missing {missing[0].isoformat()}")
    fields = {short_name: np.asarray(archive[short_name][[index[time] for time in required]], dtype=np.float64) for short_name in VARIABLES}
    return (
        np.asarray(archive["latitudes"], dtype=np.float64),
        np.asarray(archive["longitudes"], dtype=np.float64),
        fields,
        {"sha256": sha256(CURRENT_2026), "missing_hours": 0},
    )


def derive_daily(year: int, fields: dict[str, np.ndarray], retained_indices: np.ndarray, cosine_weights: np.ndarray) -> tuple[list[dict[str, object]], dict[str, int]]:
    subset = {short_name: values[:, retained_indices] for short_name, values in fields.items()}
    nonfinite = {short_name: int(np.count_nonzero(~np.isfinite(values))) for short_name, values in subset.items()}
    if any(nonfinite.values()):
        raise RuntimeError(f"Nonfinite values in retained cells for {year}: {nonfinite}")
    temperature_c = subset["2t"] - 273.15
    vpd = vpd_kpa(subset["2t"], subset["2d"])
    below_tolerance = int(np.count_nonzero(vpd < -0.005))
    if below_tolerance:
        raise RuntimeError(f"VPD below pre-specified tolerance for {year}: {below_tolerance}")
    negative_clipped = int(np.count_nonzero((vpd < 0) & (vpd >= -0.005)))
    vpd = np.maximum(vpd, 0.0)
    wind = np.hypot(subset["10u"], subset["10v"])
    temp_weighted, temp_equal = weighted_hourly(temperature_c, cosine_weights)
    vpd_weighted, vpd_equal = weighted_hourly(vpd, cosine_weights)
    wind_weighted, wind_equal = weighted_hourly(wind, cosine_weights)
    rows: list[dict[str, object]] = []
    start = datetime(year, 8, 1)
    for day in range(31):
        slice_ = slice(day * 24, (day + 1) * 24)
        day_start = start + timedelta(days=day)
        rows.append(
            {
                "season": year,
                "date_utc": day_start.date().isoformat(),
                "air_temperature_mean_c": float(np.mean(temp_weighted[slice_])),
                "air_temperature_max_c": float(np.max(temp_weighted[slice_])),
                "vapour_pressure_deficit_mean_kpa": float(np.mean(vpd_weighted[slice_])),
                "wind_speed_mean_ms": float(np.mean(wind_weighted[slice_])),
                "wind_speed_max_ms": float(np.max(wind_weighted[slice_])),
                "air_temperature_mean_c_equal_weight": float(np.mean(temp_equal[slice_])),
                "air_temperature_max_c_equal_weight": float(np.max(temp_equal[slice_])),
                "vapour_pressure_deficit_mean_kpa_equal_weight": float(np.mean(vpd_equal[slice_])),
                "wind_speed_mean_ms_equal_weight": float(np.mean(wind_equal[slice_])),
                "wind_speed_max_ms_equal_weight": float(np.max(wind_equal[slice_])),
            }
        )
    return rows, {**nonfinite, "vpd_negative_clipped": negative_clipped, "vpd_below_tolerance": below_tolerance}


def p95(values: np.ndarray) -> float:
    return float(np.percentile(values, 95, method="linear"))


def load_gwis_counts() -> dict[int, int]:
    with GWIS_HISTORY.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    counts: dict[int, int] = defaultdict(int)
    for row in rows:
        if row["intersects_tolima"].lower() == "true":
            counts[int(row["season"])] += 1
    return dict(counts)


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    for path in (DAILY_OUT, SEASON_OUT, AUDIT_OUT):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {path}")
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    retained_keys, cosine_weights = load_grid_rule()
    manifest = json.loads(HISTORICAL_MANIFEST.read_text(encoding="utf-8"))
    august_sources = {int(entry["year"]): ROOT / entry["file"] for entry in manifest["files"] if entry["period"] == "august"}
    expected_historical = set(plan["reference_seasons"])
    if set(august_sources) != expected_historical:
        raise RuntimeError("Historical manifest does not have exactly the frozen reference seasons")
    all_rows: list[dict[str, object]] = []
    source_audit: dict[str, object] = {}
    common_grid_keys: set[tuple[float, float]] | None = None
    for year in plan["seasons"]:
        if year == plan["target_season"]:
            latitudes, longitudes, fields, details = load_2026_august()
        else:
            latitudes, longitudes, fields, details = read_historical_august(year, august_sources[year])
        keys = set(key_pairs(latitudes, longitudes))
        if common_grid_keys is None:
            common_grid_keys = keys
        elif keys != common_grid_keys:
            raise RuntimeError(f"Grid coordinate set differs for {year}")
        retained_indices = apply_grid_rule(latitudes, longitudes, retained_keys)
        rows, construction = derive_daily(year, fields, retained_indices, cosine_weights)
        all_rows.extend(rows)
        source_audit[str(year)] = {**details, **construction, "retained_grid_centres": int(retained_indices.size)}
    daily_fields = list(all_rows[0])
    write_csv(DAILY_OUT, all_rows, daily_fields)
    gwis_counts = load_gwis_counts()
    metrics = [field for field in daily_fields if field not in {"season", "date_utc"}]
    baseline = [int(year) for year in plan["reference_seasons"]]
    summary_rows: list[dict[str, object]] = []
    by_year: dict[int, list[dict[str, object]]] = defaultdict(list)
    for row in all_rows:
        by_year[int(row["season"])].append(row)
    for metric in metrics:
        seasonal_mean = {year: float(np.mean([float(row[metric]) for row in by_year[year]])) for year in plan["seasons"]}
        seasonal_p95 = {year: p95(np.asarray([float(row[metric]) for row in by_year[year]], dtype=float)) for year in plan["seasons"]}
        for summary_name, values in (("mean", seasonal_mean), ("p95", seasonal_p95)):
            reference_values = np.asarray([values[year] for year in baseline], dtype=float)
            target = values[int(plan["target_season"])]
            summary_rows.append(
                {
                    "metric": metric,
                    "seasonal_summary": summary_name,
                    "season": int(plan["target_season"]),
                    "value": target,
                    "reference_median": float(np.median(reference_values)),
                    "reference_minimum": float(np.min(reference_values)),
                    "reference_maximum": float(np.max(reference_values)),
                    "difference_from_reference_median": float(target - np.median(reference_values)),
                    "target_rank_among_six": int(1 + np.count_nonzero(np.asarray([values[year] for year in plan["seasons"]]) < target)),
                    "reference_season_count": len(baseline),
                    "gwis_operational_event_count_same_season": gwis_counts.get(int(plan["target_season"])),
                }
            )
            for year in baseline:
                summary_rows.append(
                    {
                        "metric": metric,
                        "seasonal_summary": summary_name,
                        "season": year,
                        "value": values[year],
                        "reference_median": "",
                        "reference_minimum": "",
                        "reference_maximum": "",
                        "difference_from_reference_median": "",
                        "target_rank_among_six": "",
                        "reference_season_count": len(baseline),
                        "gwis_operational_event_count_same_season": gwis_counts.get(year),
                    }
                )
    season_fields = list(summary_rows[0])
    write_csv(SEASON_OUT, summary_rows, season_fields)
    audit = {
        "inference_class": plan["inference_class"],
        "plan": str(PLAN_PATH.relative_to(ROOT)).replace("\\", "/"),
        "inputs": {
            "grid_link": {"file": str(GRID_LINK.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(GRID_LINK)},
            "historical_manifest": {"file": str(HISTORICAL_MANIFEST.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(HISTORICAL_MANIFEST)},
            "current_2026": {"file": str(CURRENT_2026.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(CURRENT_2026)},
            "gwis_history": {"file": str(GWIS_HISTORY.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(GWIS_HISTORY)},
        },
        "source_construction_audit": source_audit,
        "accepted": {
            "seasons": plan["seasons"],
            "daily_rows": len(all_rows),
            "rows_per_season": {str(year): len(by_year[year]) for year in plan["seasons"]},
            "common_grid_point_count": len(common_grid_keys or set()),
            "retained_tolima_grid_centres": len(retained_keys),
            "outputs": {
                "daily": {"file": str(DAILY_OUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(DAILY_OUT)},
                "seasonal": {"file": str(SEASON_OUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(SEASON_OUT)},
            },
        },
        "event_count_interpretation_limit": plan["comparison"]["event_count_context"],
        "interpretation_limit": "This output describes the atmospheric configuration over a common set of ERA5 Land grid-cell centres. It does not establish local meteorological accuracy, wildfire occurrence, area burned, causal mechanisms, prediction, or comparability of detection sensitivity across GWIS seasons.",
    }
    AUDIT_OUT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_OUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"daily_rows": len(all_rows), "season_summary_rows": len(summary_rows), "audit": str(AUDIT_OUT.relative_to(ROOT)), "source_construction_audit": source_audit}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
