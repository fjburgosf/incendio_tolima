"""Sitúa la demanda atmosférica de agosto de 2026 en las celdas donde ocurre la actividad térmica.

Sigue `configs/fire_location_demand_plan_v1.json`, parte A, congelado antes de
calcular cualquier valor. Reutiliza los lectores y la fórmula del script 148 y
comprueba primero que reproduce su serie de Tolima completa. Después asigna las
detecciones VIIRS de Tolima al centro ERA5-Land más cercano y repite el
posicionamiento sobre las celdas con fuego.

Guarda además el déficit horario de los 989 centros en float32 para el contraste
con estaciones del script 173. Análisis descriptivo y posterior.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from collections import Counter
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "fire_location_demand_plan_v1.json"
DETECTIONS = ROOT / "data" / "interim" / "regional_fire_detections_labelled_v1.csv"
SEASON_148 = ROOT / "data" / "processed" / "era5_land_august_season_danger_1991_2026_v1.csv"
GRID_OUT = ROOT / "data" / "interim" / "era5_land_august_hourly_vpd_grid_1991_2026_v1.npz"
SEASON_OUT = ROOT / "data" / "processed" / "tolima_fire_location_demand_season_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_fire_location_demand_v1.json"
TARGET = 2026
LONG_REFERENCE = list(range(1991, 2026))
SHORT_REFERENCE = list(range(2012, 2026))


def load_148():
    spec = importlib.util.spec_from_file_location("danger148", ROOT / "scripts" / "148_build_august_fire_danger_climatology.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def season_metric(hourly: np.ndarray, weights: np.ndarray) -> tuple[float, np.ndarray]:
    """Media de los máximos diarios UTC de la serie espacial ponderada, convención del script 148."""
    series = np.average(hourly, axis=1, weights=weights)
    daily_max = series.reshape(31, 24).max(axis=1)
    return float(daily_max.mean()), daily_max


def position(observed: float, reference: list[float]) -> dict:
    values = np.asarray(reference, dtype=float)
    below = int(np.sum(values < observed))
    equal = int(np.sum(values == observed))
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    return {
        "observed": round(observed, 6),
        "reference_n": int(values.size),
        "reference_median": round(median, 6),
        "rank_ascending_with_target": int(below + 1),
        "empirical_percentile": round(100.0 * (below + 0.5 * equal) / values.size, 2),
        "robust_z": round((observed - median) / (1.4826 * mad), 4) if mad > 0 else None,
    }


def verdict(percentile: float, rule: dict) -> str:
    if percentile <= 33.3:
        return "confirms_low_demand"
    if percentile >= 66.7:
        return "contradicts"
    return "intermediate"


def main() -> None:
    if AUDIT.exists() or SEASON_OUT.exists():
        raise FileExistsError("Las salidas ya existen y no se sobrescriben")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    m148 = load_148()
    plan148 = json.loads(m148.PLAN_PATH.read_text(encoding="utf-8"))
    retained_keys, cos_weights = m148.load_grid_rule(plan148)
    tolerance = plan148["acceptance_criteria"]["vapour_pressure_deficit_minimum_tolerance_kpa"]

    reference_148 = {int(r["season"]): float(r["vpd_daily_max_mean_kpa"]) for r in csv.DictReader(SEASON_148.open(encoding="utf-8"))}

    grid = {}
    latitudes = longitudes = None
    reproduction = {}
    inputs = []
    for year in LONG_REFERENCE + [TARGET]:
        if year == TARGET:
            lat, lon, fields = m148.read_target_season()
            inputs.append({"season": year, "path": str(m148.CURRENT_2026.relative_to(ROOT)), "sha256": sha256(m148.CURRENT_2026)})
        else:
            path = m148.CLIMATOLOGY_DIR / f"era5_land_core_hourly_{year}_08_tolima_screening_bbox.grib"
            lat, lon, fields = m148.read_reference_season(year, path, plan148["acceptance_criteria"]["expected_units"])
            inputs.append({"season": year, "path": str(path.relative_to(ROOT)), "sha256": sha256(path)})
        if latitudes is None:
            latitudes, longitudes = lat, lon
        elif not (np.allclose(lat, latitudes) and np.allclose(lon, longitudes)):
            raise RuntimeError(f"Cuadrícula distinta en {year}")
        vpd = m148.vpd_kpa(fields["2t"], fields["2d"])
        indices = m148.retained_indices(lat, lon, retained_keys)
        if int(np.count_nonzero(vpd[:, indices] < tolerance)):
            raise RuntimeError(f"Déficit por debajo de la tolerancia en {year}")
        vpd = np.maximum(vpd, 0.0)
        value, _ = season_metric(vpd[:, indices], cos_weights)
        reproduction[year] = abs(value - reference_148[year])
        grid[year] = vpd.astype(np.float32)
    worst = max(reproduction.values())
    if worst > 1e-9:
        raise RuntimeError(f"No se reproduce la serie del script 148, diferencia máxima {worst}")

    indices = m148.retained_indices(latitudes, longitudes, retained_keys)
    key_to_column = {key: column for column, key in enumerate(retained_keys)}

    counts: dict[int, Counter] = {year: Counter() for year in range(2012, TARGET + 1)}
    unassigned = Counter()
    with DETECTIONS.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["department"] != "Tolima":
                continue
            season = int(row["season"])
            lat, lon = float(row["latitude"]), float(row["longitude"])
            key = (round(round(lat * 10) / 10, 6), round(round(lon * 10) / 10, 6))
            if abs(lat - key[0]) > 0.05 + 1e-9 or abs(lon - key[1]) > 0.05 + 1e-9 or key not in key_to_column:
                unassigned[season] += 1
                continue
            counts[season][key_to_column[key]] += 1

    reference_counts = Counter()
    for year in SHORT_REFERENCE:
        reference_counts.update(counts[year])
    primary_columns = np.asarray(sorted(reference_counts), dtype=int)
    q75 = float(np.percentile([reference_counts[c] for c in primary_columns], 75))
    upper_columns = np.asarray([c for c in primary_columns if reference_counts[c] >= q75], dtype=int)

    masks = {
        "all_tolima_198": (np.arange(len(retained_keys)), None),
        "fire_prone_reference": (primary_columns, None),
        "fire_prone_upper_quartile": (upper_columns, None),
    }
    season_rows = []
    daily_primary = {}
    for year in LONG_REFERENCE + [TARGET]:
        retained_hourly = grid[year][:, indices].astype(np.float64)
        row = {"season": year}
        for name, (columns, _) in masks.items():
            value, daily = season_metric(retained_hourly[:, columns], cos_weights[columns])
            row[f"vpd_daily_max_mean_kpa_{name}"] = value
            if name == "fire_prone_reference":
                daily_primary[year] = daily
        if year >= 2012:
            weights = np.zeros(len(retained_keys))
            for column, n in counts[year].items():
                weights[column] = n
            if weights.sum() > 0:
                value, _ = season_metric(retained_hourly, weights)
                row["vpd_daily_max_mean_kpa_detection_weighted_same_season"] = value
            row["tolima_detections_assigned"] = int(sum(counts[year].values()))
            row["tolima_detections_unassigned"] = int(unassigned[year])
        season_rows.append(row)

    by_year = {row["season"]: row for row in season_rows}
    positioning = {}
    for name in [*masks, "detection_weighted_same_season"]:
        column = f"vpd_daily_max_mean_kpa_{name}"
        observed = by_year[TARGET][column]
        result = {}
        if name != "detection_weighted_same_season":
            result["reference_1991_2025"] = position(observed, [by_year[y][column] for y in LONG_REFERENCE])
        result["reference_2012_2025"] = position(observed, [by_year[y][column] for y in SHORT_REFERENCE])
        positioning[name] = result

    reference_daily = np.concatenate([daily_primary[y] for y in SHORT_REFERENCE])
    threshold = float(np.percentile(reference_daily, 90))
    days_above = {y: int(np.sum(daily_primary[y] > threshold)) for y in range(2012, TARGET + 1)}
    all_vs_fire_corr = float(np.corrcoef(
        [by_year[y]["vpd_daily_max_mean_kpa_all_tolima_198"] for y in LONG_REFERENCE + [TARGET]],
        [by_year[y]["vpd_daily_max_mean_kpa_fire_prone_reference"] for y in LONG_REFERENCE + [TARGET]],
    )[0, 1])

    primary_percentile = positioning["fire_prone_reference"]["reference_1991_2025"]["empirical_percentile"]

    GRID_OUT.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        GRID_OUT,
        seasons=np.asarray(LONG_REFERENCE + [TARGET], dtype=np.int16),
        vpd_kpa=np.stack([grid[y] for y in LONG_REFERENCE + [TARGET]]),
        latitudes=latitudes,
        longitudes=longitudes,
        hour_note=np.asarray("hourly from August 1 00 UTC to August 31 23 UTC"),
    )
    SEASON_OUT.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in season_rows for key in row}, key=lambda k: (k != "season", k))
    with SEASON_OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(season_rows)

    audit = {
        "script": Path(__file__).name,
        "plan": str(PLAN.relative_to(ROOT)).replace("\\", "/"),
        "plan_sha256": sha256(PLAN),
        "status": "descriptivo, posterior, declarado",
        "reproduction_of_script_148_max_abs_difference_kpa": worst,
        "cells": {
            "retained_tolima": len(retained_keys),
            "fire_prone_reference": int(primary_columns.size),
            "fire_prone_upper_quartile": int(upper_columns.size),
            "upper_quartile_threshold_detections": q75,
        },
        "detections_unassigned_by_season": {str(k): v for k, v in sorted(unassigned.items())},
        "positioning_2026": positioning,
        "primary_verdict": verdict(primary_percentile, plan["part_a_fire_location_reanalysis"]["decision_rule"]),
        "daily_threshold_fire_prone_p90_kpa": round(threshold, 6),
        "days_above_threshold_by_season": days_above,
        "pearson_all_tolima_vs_fire_prone_1991_2026": round(all_vs_fire_corr, 4),
        "inputs": inputs + [{"path": str(DETECTIONS.relative_to(ROOT)), "sha256": sha256(DETECTIONS)}],
        "outputs": [str(SEASON_OUT.relative_to(ROOT)), str(GRID_OUT.relative_to(ROOT))],
        "limits": [
            "La máscara primaria se define con detecciones de 2012 a 2025 y no con 2026.",
            "El reanálisis a 0.1 grados suaviza el contraste entre valle y ladera dentro de una celda.",
            "Análisis añadido después de conocer los resultados estacionales.",
        ],
    }
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: audit[k] for k in ("reproduction_of_script_148_max_abs_difference_kpa", "cells",
                                            "detections_unassigned_by_season", "positioning_2026", "primary_verdict",
                                            "daily_threshold_fire_prone_p90_kpa", "days_above_threshold_by_season",
                                            "pearson_all_tolima_vs_fire_prone_1991_2026")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
