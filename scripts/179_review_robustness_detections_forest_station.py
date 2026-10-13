"""Robustez del ranking VIIRS, acuerdo de coberturas, error de estación y altitud del bosque.

Sigue `configs/review_robustness_plan_v1.json`, partes A, B y C, y la parte A de
`configs/forest_validation_plan_v1.json`, congelados antes de ejecutar. Todos los
análisis son descriptivos y posteriores a la revisión simulada del 12 de octubre de 2026.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from pyproj import Transformer


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "review_robustness_plan_v1.json"
FOREST_PLAN = ROOT / "configs" / "forest_validation_plan_v1.json"
DETECTIONS = ROOT / "data" / "interim" / "regional_fire_detections_labelled_v1.csv"
COVER_ROWS = ROOT / "data" / "processed" / "tolima_detection_cover_v1.csv"
GRID = ROOT / "data" / "interim" / "era5_land_august_hourly_vpd_grid_1991_2026_v1.npz"
STATION_RAW = ROOT / "data" / "raw" / "ideam_hourly_temperature_humidity_2026-10-12"
AUDIT = ROOT / "results" / "exploratory" / "audit_review_robustness_detections_forest_station_v1.json"
SEASONS = list(range(2012, 2027))
TARGET = 2026
CAUTION_DAYS = {"2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-17"}
IMPUTE_DAYS = ["2026-08-03", "2026-08-05", "2026-08-06", "2026-08-17"]
NATAIMA = "0021185090"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name.split("_")[0] + "_mod", ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def rank_desc(values: dict[int, float], season: int = TARGET) -> int:
    return int(sum(v > values[season] for s, v in values.items() if s != season) + 1)


def part_a() -> dict:
    tolima, regional_days = [], defaultdict(set)
    with DETECTIONS.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            regional_days[int(row["season"])].add(row["acq_date"])
            if row["department"] == "Tolima":
                tolima.append(row)
    common = {d[5:] for d in CAUTION_DAYS}

    def by_season(rows, metric):
        out = {s: 0.0 for s in SEASONS}
        cells = defaultdict(set)
        for r in rows:
            s = int(r["season"])
            if metric == "cells":
                cells[s].add((r["cell_x"], r["cell_y"]))
            else:
                out[s] += 1
        return {s: float(len(cells[s])) for s in SEASONS} if metric == "cells" else out

    observed = by_season(tolima, "count")
    common_rows = [r for r in tolima if r["acq_date"][5:] not in common]
    daily_2026 = Counter(r["acq_date"] for r in tolima if r["season"] == "2026")
    clear_days = [f"2026-08-{d:02d}" for d in range(1, 32) if f"2026-08-{d:02d}" not in CAUTION_DAYS]
    mean_clear = float(np.mean([daily_2026.get(d, 0) for d in clear_days]))
    max_clear = float(max(daily_2026.get(d, 0) for d in clear_days))
    scenarios = {
        "observed": observed,
        "common_days": by_season(common_rows, "count"),
        "imputed_mean": {**observed, TARGET: observed[TARGET] + mean_clear * len(IMPUTE_DAYS)},
        "imputed_max": {**observed, TARGET: observed[TARGET] + max_clear * len(IMPUTE_DAYS)},
        "nominal_high_confidence": by_season([r for r in tolima if r["confidence"] in ("n", "h")], "count"),
        "per_observed_day": {s: observed[s] / len(regional_days[s]) for s in SEASONS},
        "cells": by_season(tolima, "cells"),
        "cells_common_days": by_season(common_rows, "cells"),
    }
    result = {}
    for name, values in scenarios.items():
        ordered = sorted(values.items(), key=lambda kv: -kv[1])
        result[name] = {"rank_2026_of_15": rank_desc(values), "value_2026": round(values[TARGET], 3),
                        "top_three": [[s, round(v, 3)] for s, v in ordered[:3]]}
    result["regional_days_with_detection"] = {str(s): len(regional_days[s]) for s in SEASONS}
    result["imputation_daily_mean_and_max_2026"] = [round(mean_clear, 3), max_clear]
    ranks = [v["rank_2026_of_15"] for k, v in result.items() if isinstance(v, dict) and "rank_2026_of_15" in v]
    result["rank_range"] = [min(ranks), max(ranks)]
    result["decision"] = "second_in_all" if set(ranks) == {2} else "report_rank_range"
    return result


def part_b() -> dict:
    rows = list(csv.DictReader(COVER_ROWS.open(encoding="utf-8")))
    groups = [k.replace("window_fraction_", "") for k in rows[0] if k.startswith("window_fraction_")]
    disagree, by_season_forest = 0, defaultdict(list)
    for r in rows:
        fractions = {g: float(r[f"window_fraction_{g}"]) for g in groups}
        majority = max(fractions, key=fractions.get)
        disagree += majority != r["cover_group"]
        if r["cover_group"] == "forest_formation":
            by_season_forest[int(r["season"])].append(fractions)
    target = by_season_forest[TARGET]
    edge = [f["forest_formation"] >= 0.10 and f["agricultural_other"] >= 0.10 for f in target]
    edge_reference = [f["forest_formation"] >= 0.10 and f["agricultural_other"] >= 0.10
                      for s in SEASONS if s != TARGET for f in by_season_forest[s]]
    return {
        "detections": len(rows),
        "point_vs_window_majority_disagreement_fraction": round(disagree / len(rows), 4),
        "forest_point_2026": len(target),
        "forest_point_2026_window_forest_at_least_half": round(float(np.mean([f["forest_formation"] >= 0.5 for f in target])), 4),
        "forest_point_2026_edge_fraction": round(float(np.mean(edge)), 4),
        "forest_point_2012_2025_edge_fraction": round(float(np.mean(edge_reference)), 4),
        "forest_point_2026_mean_window_agricultural_fraction": round(float(np.mean([f["agricultural_other"] for f in target])), 4),
    }


def part_c() -> dict:
    m173 = load("173_ideam_station_demand_check.py")
    grid = np.load(GRID)
    seasons = [int(s) for s in grid["seasons"]]
    hourly = {"temperature": defaultdict(list), "humidity": defaultdict(list)}
    for variable in hourly:
        for row in json.loads((STATION_RAW / f"{variable}_{NATAIMA}.json").read_text(encoding="utf-8")):
            value = float(row["valorobservado"])
            ok = (-5 <= value <= 45) if variable == "temperature" else (0 < value <= 100)
            if ok:
                stamp = datetime.fromisoformat(row["fechaobservacion"][:19]).replace(minute=0, second=0)
                hourly[variable][stamp].append(value)
    first = json.loads((STATION_RAW / f"temperature_{NATAIMA}.json").read_text(encoding="utf-8"))
    del first
    audit173 = json.loads((ROOT / "results" / "exploratory" / "audit_ideam_station_demand_v1.json").read_text(encoding="utf-8"))
    meta = [s for s in audit173["stations"] if s["station_code"] == NATAIMA][0]
    nearest = int(np.argmin((grid["latitudes"] - meta["latitude"]) ** 2 + (grid["longitudes"] - meta["longitude"]) ** 2))
    out = {}
    for year in m173.YEARS:
        start = datetime(year, 8, m173.FIRST_DAY)
        hours = [start + timedelta(hours=h) for h in range(m173.WINDOW_HOURS)]
        t = np.array([np.mean(hourly["temperature"][h]) if hourly["temperature"].get(h) else np.nan for h in hours])
        rh = np.array([np.mean(hourly["humidity"][h]) if hourly["humidity"].get(h) else np.nan for h in hours])
        if (np.isfinite(t) & np.isfinite(rh)).sum() < 0.8 * m173.WINDOW_HOURS or year not in seasons:
            continue
        values = m173.vpd(t, rh).reshape(-1, 24)
        valid = np.isfinite(values).sum(axis=1) >= 18
        station = np.where(valid, np.nanmax(np.where(np.isfinite(values), values, -np.inf), axis=1), np.nan)
        cube = grid["vpd_kpa"][seasons.index(year)][:, nearest].astype(float)
        local = {datetime(year, 8, 1) + timedelta(hours=h) - timedelta(hours=5): v for h, v in enumerate(cube)}
        era5 = []
        for d in range(len(station)):
            day = datetime(year, 8, m173.FIRST_DAY + d)
            vals = [local[day + timedelta(hours=h)] for h in range(24) if day + timedelta(hours=h) in local]
            era5.append(max(vals) if vals and np.isfinite(station[d]) else np.nan)
        era5 = np.asarray(era5)
        both = np.isfinite(station) & np.isfinite(era5)
        error = era5[both] - station[both]
        out[str(year)] = {"days": int(both.sum()), "bias_kpa": round(float(error.mean()), 4),
                          "mae_kpa": round(float(np.abs(error).mean()), 4), "rmse_kpa": round(float(np.sqrt((error ** 2).mean())), 4),
                          "station_mean_kpa": round(float(station[both].mean()), 4), "era5_mean_kpa": round(float(era5[both].mean()), 4)}
    return {"station": meta["station_name"], "nearest_grid_index": nearest, "by_year": out}


def part_forest_elevation() -> dict:
    m162 = load("162_build_spatial_case_control_panel.py")
    tiles = m162.dem_tiles()
    forward = Transformer.from_crs("EPSG:4326", m162.CRS_PROJECTED, always_xy=True)
    inverse = Transformer.from_crs(m162.CRS_PROJECTED, "EPSG:4326", always_xy=True)
    elevations = defaultdict(list)
    missing = 0
    for r in csv.DictReader(COVER_ROWS.open(encoding="utf-8")):
        if r["cover_group"] != "forest_formation":
            continue
        x, y = forward.transform(float(r["longitude"]), float(r["latitude"]))
        sample = m162.dem_value_and_slope(x, y, tiles, inverse)
        if sample is None:
            missing += 1
            continue
        elevations[int(r["season"])].append(sample[0])
    summary = {}
    for s in SEASONS:
        v = np.asarray(elevations[s])
        summary[str(s)] = {"n": int(v.size), "median_m": round(float(np.median(v)), 1), "q25_m": round(float(np.quantile(v, 0.25)), 1),
                           "q75_m": round(float(np.quantile(v, 0.75)), 1), "fraction_below_1000_m": round(float(np.mean(v < 1000)), 4)}
    ref = [summary[str(s)]["fraction_below_1000_m"] for s in SEASONS if s != TARGET]
    obs = summary[str(TARGET)]["fraction_below_1000_m"]
    below, equal = sum(x < obs for x in ref), sum(x == obs for x in ref)
    return {"by_season": summary, "missing_dem": missing,
            "fraction_below_1000_m_2026_percentile": round(100 * (below + 0.5 * equal) / len(ref), 2)}


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribe")
    audit = {
        "script": Path(__file__).name,
        "plan_sha256": sha256(PLAN),
        "forest_plan_sha256": sha256(FOREST_PLAN),
        "status": "descriptivo y posterior a la revisión simulada",
        "A_viirs_ranking_robustness": part_a(),
        "B_forest_assignment_agreement": part_b(),
        "C_station_error_nataima": part_c(),
        "forest_elevation": part_forest_elevation(),
        "inputs": [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(p)} for p in (DETECTIONS, COVER_ROWS, GRID)],
    }
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=lambda o: o.item()) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=1, default=lambda o: o.item()))


if __name__ == "__main__":
    main()
