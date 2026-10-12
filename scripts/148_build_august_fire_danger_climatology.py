"""Construye métricas diarias de peligro atmosférico de agosto entre 1991 y 2026.

Los criterios quedaron congelados en
`configs/august_fire_danger_climatology_plan_v1.json` antes de ejecutar.

El artefacto sustituye el promedio mensual de déficit de presión de vapor por
máximos diarios, valor de tarde y conteo de días secos. El umbral de día seco se
calcula solo con las temporadas de referencia, de modo que la temporada evaluada
no participa en la definición de su propio umbral.

La regla espacial de 198 centros y la función de déficit de presión de vapor son
las ya congeladas en el script 129 y se reutilizan sin modificación.
"""

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
PLAN_PATH = ROOT / "configs" / "august_fire_danger_climatology_plan_v1.json"
GRID_LINK = ROOT / "data" / "interim" / "era5_land_grid_municipality_linkage.csv"
CLIMATOLOGY_DIR = ROOT / "data" / "raw" / "era5_land" / "august_climatology_1991_2025"
CURRENT_2026 = ROOT / "data" / "interim" / "era5_land_hourly_grid_v1.npz"
DAILY_OUT = ROOT / "data" / "processed" / "era5_land_august_daily_danger_1991_2026_v1.csv"
SEASON_OUT = ROOT / "data" / "processed" / "era5_land_august_season_danger_1991_2026_v1.csv"
AUDIT_OUT = ROOT / "results" / "primary" / "audit_august_fire_danger_climatology_v1.json"
VARIABLES = ("2t", "2d", "10u", "10v")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def expected_times(year: int) -> list[datetime]:
    start = datetime(year, 8, 1)
    return [start + timedelta(hours=index) for index in range(31 * 24)]


def vpd_kpa(air_temperature_k: np.ndarray, dewpoint_k: np.ndarray) -> np.ndarray:
    air_c = air_temperature_k - 273.15
    dew_c = dewpoint_k - 273.15
    saturation = 0.6108 * np.exp((17.27 * air_c) / (air_c + 237.3))
    actual = 0.6108 * np.exp((17.27 * dew_c) / (dew_c + 237.3))
    return saturation - actual


def load_grid_rule(plan: dict) -> tuple[list[tuple[float, float]], np.ndarray]:
    rule = plan["spatial_rule"]
    with GRID_LINK.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    inside = [row for row in rows if row["municipality_match_count"] == "1"]
    keys = [(round(float(row["latitude"]), 6), round(float(row["longitude"]), 6)) for row in inside]
    if len(rows) != rule["total_centres"] or len(inside) != rule["retained_centres"] or len(set(keys)) != len(keys):
        raise RuntimeError("La regla congelada de centros de Tolima ya no reproduce 989 totales y 198 retenidos")
    return keys, np.cos(np.deg2rad(np.asarray([latitude for latitude, _ in keys], dtype=np.float64)))


def retained_indices(latitudes: np.ndarray, longitudes: np.ndarray, retained_keys: list) -> np.ndarray:
    keys = [(round(float(lat), 6), round(float(lon), 6)) for lat, lon in zip(latitudes, longitudes)]
    if len(keys) != 989 or len(set(keys)) != 989:
        raise RuntimeError("La cuadrícula ERA5 no tiene los 989 centros únicos esperados")
    index_by_key = {key: position for position, key in enumerate(keys)}
    missing = [key for key in retained_keys if key not in index_by_key]
    if missing:
        raise RuntimeError(f"Centro retenido ausente en la fuente, primero {missing[0]}")
    return np.asarray([index_by_key[key] for key in retained_keys], dtype=int)


def read_reference_season(year: int, path: Path, expected_units: dict) -> tuple[np.ndarray, np.ndarray, dict]:
    records: dict[str, dict[datetime, np.ndarray]] = defaultdict(dict)
    latitudes = longitudes = None
    units: dict[str, set[str]] = defaultdict(set)
    with path.open("rb") as handle:
        while message := eccodes.codes_grib_new_from_file(handle):
            try:
                short_name = str(eccodes.codes_get(message, "shortName"))
                if short_name not in VARIABLES:
                    raise RuntimeError(f"Variable inesperada {short_name} en {path.name}")
                valid_date = str(eccodes.codes_get(message, "validityDate"))
                valid_time = int(eccodes.codes_get(message, "validityTime"))
                valid = datetime.strptime(f"{valid_date}{valid_time:04d}", "%Y%m%d%H%M")
                if valid in records[short_name]:
                    raise RuntimeError(f"Registro horario duplicado en {path.name}, {short_name} {valid.isoformat()}")
                values = np.asarray(eccodes.codes_get_array(message, "values"), dtype=np.float64)
                current_lat = np.asarray(eccodes.codes_get_array(message, "latitudes"), dtype=np.float64)
                current_lon = np.asarray(eccodes.codes_get_array(message, "longitudes"), dtype=np.float64)
                if latitudes is None:
                    latitudes, longitudes = current_lat, current_lon
                elif not (np.array_equal(current_lat, latitudes) and np.array_equal(current_lon, longitudes)):
                    raise RuntimeError(f"Cuadrícula inconsistente dentro de {path.name}")
                records[short_name][valid] = values
                units[short_name].add(str(eccodes.codes_get(message, "units")))
            finally:
                eccodes.codes_release(message)
    if latitudes is None:
        raise RuntimeError(f"No se leyó ningún mensaje GRIB en {path}")
    times = expected_times(year)
    fields = {}
    for short_name in VARIABLES:
        missing = sorted(set(times) - set(records[short_name]))
        unexpected = sorted(set(records[short_name]) - set(times))
        if missing or unexpected:
            raise RuntimeError(f"Cobertura horaria incompleta en {year} para {short_name}")
        if units[short_name] != {expected_units[short_name]}:
            raise RuntimeError(f"Unidad inesperada en {year} para {short_name}, {units[short_name]}")
        fields[short_name] = np.stack([records[short_name][time] for time in times], axis=0)
    return latitudes, longitudes, fields


def read_target_season() -> tuple[np.ndarray, np.ndarray, dict]:
    archive = np.load(CURRENT_2026)
    times = [datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ") for value in archive["validity_utc"]]
    required = expected_times(2026)
    index = {time: position for position, time in enumerate(times)}
    missing = [time for time in required if time not in index]
    if missing:
        raise RuntimeError(f"Agosto de 2026 está incompleto, primera hora ausente {missing[0].isoformat()}")
    order = [index[time] for time in required]
    fields = {name: np.asarray(archive[name][order], dtype=np.float64) for name in VARIABLES}
    return (
        np.asarray(archive["latitudes"], dtype=np.float64),
        np.asarray(archive["longitudes"], dtype=np.float64),
        fields,
    )


def derive_daily(year: int, fields: dict, indices: np.ndarray, weights: np.ndarray, plan: dict) -> tuple[list[dict], dict]:
    tolerance = plan["acceptance_criteria"]["vapour_pressure_deficit_minimum_tolerance_kpa"]
    afternoon = plan["temporal_rule"]["afternoon_hour_utc"]
    subset = {name: values[:, indices] for name, values in fields.items()}
    nonfinite = {name: int(np.count_nonzero(~np.isfinite(values))) for name, values in subset.items()}
    if any(nonfinite.values()):
        raise RuntimeError(f"Valores no finitos en celdas retenidas para {year}, {nonfinite}")
    vpd = vpd_kpa(subset["2t"], subset["2d"])
    if int(np.count_nonzero(vpd < tolerance)):
        raise RuntimeError(f"Déficit de presión de vapor por debajo de la tolerancia fijada en {year}")
    clipped = int(np.count_nonzero((vpd < 0) & (vpd >= tolerance)))
    vpd = np.maximum(vpd, 0.0)
    temperature = subset["2t"] - 273.15
    wind = np.hypot(subset["10u"], subset["10v"])
    series = {
        "vpd": np.average(vpd, axis=1, weights=weights),
        "vpd_equal": np.mean(vpd, axis=1),
        "temperature": np.average(temperature, axis=1, weights=weights),
        "wind": np.average(wind, axis=1, weights=weights),
    }
    rows = []
    start = datetime(year, 8, 1)
    for day in range(31):
        window = slice(day * 24, (day + 1) * 24)
        rows.append(
            {
                "season": year,
                "date_utc": (start + timedelta(days=day)).date().isoformat(),
                "vapour_pressure_deficit_mean_kpa": float(np.mean(series["vpd"][window])),
                "vapour_pressure_deficit_max_kpa": float(np.max(series["vpd"][window])),
                "vapour_pressure_deficit_afternoon_kpa": float(series["vpd"][day * 24 + afternoon]),
                "vapour_pressure_deficit_max_kpa_equal_weight": float(np.max(series["vpd_equal"][window])),
                "air_temperature_mean_c": float(np.mean(series["temperature"][window])),
                "air_temperature_max_c": float(np.max(series["temperature"][window])),
                "wind_speed_mean_ms": float(np.mean(series["wind"][window])),
                "wind_speed_max_ms": float(np.max(series["wind"][window])),
            }
        )
    return rows, {"vpd_negative_clipped": clipped, **nonfinite}


def longest_run(flags: list[bool]) -> int:
    best = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        best = max(best, current)
    return best


def main() -> None:
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    criteria = plan["acceptance_criteria"]
    first = plan["reference_seasons"]["first"]
    last = plan["reference_seasons"]["last"]
    target = plan["target_season"]
    reference_years = list(range(first, last + 1))

    retained_keys, weights = load_grid_rule(plan)

    daily_rows: list[dict] = []
    diagnostics: dict[int, dict] = {}
    inputs: list[dict] = []

    for year in reference_years:
        path = CLIMATOLOGY_DIR / f"era5_land_core_hourly_{year}_08_tolima_screening_bbox.grib"
        if not path.exists():
            raise RuntimeError(f"Falta el archivo de la temporada de referencia {year}")
        latitudes, longitudes, fields = read_reference_season(year, path, criteria["expected_units"])
        indices = retained_indices(latitudes, longitudes, retained_keys)
        rows, diagnostic = derive_daily(year, fields, indices, weights, plan)
        daily_rows.extend(rows)
        diagnostics[year] = diagnostic
        inputs.append({"season": year, "path": str(path.relative_to(ROOT)), "sha256": sha256(path)})

    latitudes, longitudes, fields = read_target_season()
    indices = retained_indices(latitudes, longitudes, retained_keys)
    rows, diagnostic = derive_daily(target, fields, indices, weights, plan)
    daily_rows.extend(rows)
    diagnostics[target] = diagnostic
    inputs.append({"season": target, "path": str(CURRENT_2026.relative_to(ROOT)), "sha256": sha256(CURRENT_2026)})

    reference_maxima = [row["vapour_pressure_deficit_max_kpa"] for row in daily_rows if row["season"] != target]
    threshold = float(np.percentile(np.asarray(reference_maxima), 90, method="linear"))

    season_rows = []
    for year in reference_years + [target]:
        subset = [row for row in daily_rows if row["season"] == year]
        maxima = np.asarray([row["vapour_pressure_deficit_max_kpa"] for row in subset])
        dry_flags = [value > threshold for value in maxima]
        season_rows.append(
            {
                "season": year,
                "is_reference_season": year != target,
                "vpd_daily_max_mean_kpa": float(np.mean(maxima)),
                "vpd_daily_max_p90_kpa": float(np.percentile(maxima, 90, method="linear")),
                "vpd_daily_max_season_max_kpa": float(np.max(maxima)),
                "vpd_afternoon_mean_kpa": float(np.mean([row["vapour_pressure_deficit_afternoon_kpa"] for row in subset])),
                "vpd_daily_mean_mean_kpa": float(np.mean([row["vapour_pressure_deficit_mean_kpa"] for row in subset])),
                "dry_days_count": int(sum(dry_flags)),
                "dry_days_longest_run": longest_run(dry_flags),
                "air_temperature_max_mean_c": float(np.mean([row["air_temperature_max_c"] for row in subset])),
                "wind_speed_mean_ms": float(np.mean([row["wind_speed_mean_ms"] for row in subset])),
            }
        )

    positioning = {}
    for metric in ("vpd_daily_max_mean_kpa", "vpd_daily_max_p90_kpa", "vpd_afternoon_mean_kpa",
                   "vpd_daily_mean_mean_kpa", "dry_days_count", "air_temperature_max_mean_c"):
        reference_values = np.asarray([row[metric] for row in season_rows if row["is_reference_season"]], dtype=float)
        observed = float([row[metric] for row in season_rows if row["season"] == target][0])
        median = float(np.median(reference_values))
        mad = float(np.median(np.abs(reference_values - median)))
        below = int(np.count_nonzero(reference_values < observed))
        equal = int(np.count_nonzero(reference_values == observed))
        positioning[metric] = {
            "observed_2026": observed,
            "reference_seasons": int(reference_values.size),
            "reference_median": median,
            "reference_minimum": float(np.min(reference_values)),
            "reference_maximum": float(np.max(reference_values)),
            "seasons_below_2026": below,
            "empirical_percentile": round(100.0 * (below + 0.5 * equal) / reference_values.size, 2),
            "rank_among_all_seasons_ascending": int(
                sorted([row[metric] for row in season_rows]).index(observed) + 1
            ),
            "robust_z": round((observed - median) / (1.4826 * mad), 4) if mad > 0 else None,
        }

    DAILY_OUT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_OUT.parent.mkdir(parents=True, exist_ok=True)
    with DAILY_OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(daily_rows[0].keys()))
        writer.writeheader()
        writer.writerows(daily_rows)
    with SEASON_OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(season_rows[0].keys()))
        writer.writeheader()
        writer.writerows(season_rows)

    audit = {
        "script": Path(__file__).name,
        "executed": "2026-09-09",
        "plan": {"path": str(PLAN_PATH.relative_to(ROOT)), "sha256": sha256(PLAN_PATH)},
        "reference_seasons": len(reference_years),
        "target_season": target,
        "daily_rows": len(daily_rows),
        "hours_verified_per_season": 744,
        "retained_centres": len(retained_keys),
        "dry_day_threshold_kpa": round(threshold, 6),
        "dry_day_threshold_source": plan["dry_day_threshold"]["definition"],
        "diagnostics_by_season": {str(year): value for year, value in diagnostics.items()},
        "positioning_2026": positioning,
        "checks": {
            "all_reference_seasons_present": True,
            "no_missing_hours": True,
            "no_nonfinite_values_in_retained_cells": True,
            "threshold_excludes_target_season": True,
        },
        "interpretation_limits": plan["interpretation_limits"],
        "inputs": inputs,
        "outputs": [str(DAILY_OUT.relative_to(ROOT)), str(SEASON_OUT.relative_to(ROOT))],
    }
    AUDIT_OUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"reference_seasons": len(reference_years),
                      "dry_day_threshold_kpa": round(threshold, 6),
                      "positioning_2026": positioning}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
