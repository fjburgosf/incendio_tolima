"""Días faltantes con NOAA-20 y NOAA-21, tasas por superficie de bosque y bootstrap espacial.

Sigue las partes K, L y M de `configs/review_robustness_plan_v3.json`, congeladas
antes de ejecutar. Corrige la robustez del script 179, que trataba el 17 de agosto
como día perdido y usaba un denominador de días con detección regional. Añade
denominadores de superficie para los bordes y la altitud del bosque y un bootstrap
de bloques para el modelo espacial. Descriptivo y posterior.
"""

from __future__ import annotations

import csv
import glob
import hashlib
import importlib.util
import json
import math
import warnings
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy import stats
from shapely import contains_xy
from shapely.geometry import shape
from shapely.ops import unary_union
from statsmodels.discrete.conditional_models import ConditionalLogit


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "review_robustness_plan_v3.json"
DETECTIONS = ROOT / "data" / "interim" / "regional_fire_detections_labelled_v1.csv"
COVER_ROWS = ROOT / "data" / "processed" / "tolima_detection_cover_v1.csv"
COVER_SEASONS = ROOT / "results" / "exploratory" / "tolima_detection_cover_by_season_v1.csv"
GEOB = ROOT / "data" / "raw" / "boundaries" / "geoBoundaries-COL-ADM1.geojson"
MUNICIPAL = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
RASTERS = ROOT / "data" / "raw" / "mapbiomas" / "coverage_collection3"
PANEL = ROOT / "data" / "processed" / "tolima_spatial_case_control_panel_cover_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_review_v3_missing_days_area_rates_blocks_v1.json"
SEASONS = list(range(2012, 2027))
TARGET = 2026
FOREST = {3, 5, 6, 49}
AGRI = {14, 21, 35, 74}
NATURAL = FOREST | {11, 12, 13, 29, 32, 50, 81, 82}
HALF = 6


def load(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def rank_desc(values: dict[int, float]) -> int:
    return int(sum(v > values[TARGET] for s, v in values.items() if s != TARGET) + 1)


def part_k(plan: dict) -> dict:
    k = plan["K_missing_days_v2"]
    outage, caution = set(k["classification"]["documented_outage"]), set(k["classification"]["caution_with_data"])
    tolima_geom = [shape(f["geometry"]) for f in json.loads(GEOB.read_text(encoding="utf-8"))["features"] if f["properties"]["shapeName"] == "Tolima"][0]
    noaa = {"noaa20": Counter(), "noaa21": Counter()}
    for sensor in noaa:
        for path in sorted(glob.glob(str(ROOT / "data" / "raw" / "firms" / f"viirs_{sensor}_nrt_2026-08-*.csv"))):
            rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
            lon = np.array([float(r["longitude"]) for r in rows]); lat = np.array([float(r["latitude"]) for r in rows])
            inside = contains_xy(tolima_geom, lon, lat) if rows else np.array([], bool)
            for r, ok in zip(rows, inside):
                if ok:
                    noaa[sensor][r["acq_date"]] += 1
    tolima = [r for r in csv.DictReader(DETECTIONS.open(encoding="utf-8-sig")) if r["department"] == "Tolima"]
    snpp_2026 = Counter(r["acq_date"] for r in tolima if r["season"] == "2026")
    days = [f"2026-08-{d:02d}" for d in range(1, 32)]
    reference_days = [d for d in days if d not in outage | caution]
    noaa_mean = {d: (noaa["noaa20"][d] + noaa["noaa21"][d]) / 2 for d in days}
    ratio = sum(snpp_2026[d] for d in reference_days) / sum(noaa_mean[d] for d in reference_days)
    imputed = {d: round(noaa_mean[d] * ratio, 2) for d in sorted(outage)}
    observed = {s: float(sum(1 for r in tolima if int(r["season"]) == s)) for s in SEASONS}
    common_excluded = {"08-03", "08-04", "08-05", "08-06"}
    common = {s: float(sum(1 for r in tolima if int(r["season"]) == s and r["acq_date"][5:] not in common_excluded)) for s in SEASONS}
    scenarios = {"observed": observed, "imputed_from_noaa": {**observed, TARGET: observed[TARGET] + sum(imputed.values())}, "common_days_3_to_6": common}
    return {
        "noaa_tolima_daily_2026": {s: {d[-2:]: noaa[s][d] for d in days} for s in noaa},
        "valid_zero_check_2026_08_17": {"snpp": snpp_2026["2026-08-17"], "noaa20": noaa["noaa20"]["2026-08-17"], "noaa21": noaa["noaa21"]["2026-08-17"]},
        "snpp_to_noaa_ratio": round(ratio, 4),
        "imputed_snpp_tolima_outage_days": imputed,
        "imputed_total": round(sum(imputed.values()), 2),
        "scenarios": {k2: {"value_2026": round(v[TARGET], 2), "rank_2026_of_15": rank_desc(v)} for k2, v in scenarios.items()},
    }


def fast_window(raster, lon: float, lat: float) -> np.ndarray:
    col = int(np.floor((lon - raster.x0) / raster.dx)); row = int(np.floor((raster.y0 - lat) / raster.dy))
    ty, tx = row // raster.tile_h, col // raster.tile_w
    r0, c0 = row % raster.tile_h, col % raster.tile_w
    if HALF <= r0 < raster.tile_h - HALF and HALF <= c0 < raster.tile_w - HALF:
        return raster.tile(ty, tx)[r0 - HALF:r0 + HALF + 1, c0 - HALF:c0 + HALF + 1]
    return raster.window(lon, lat, HALF)


def part_l(plan: dict) -> dict:
    m177 = load("177_forest_cover_at_detections.py", "c177")
    m162 = load("162_build_spatial_case_control_panel.py", "s162")
    tolima = unary_union([shape(f["geometry"]) for f in json.loads(MUNICIPAL.read_text(encoding="utf-8"))["features"]])
    rng = np.random.default_rng(20261014)
    west, south, east, north = tolima.bounds
    pts = []
    while len(pts) < 50000:
        lon = rng.uniform(west, east, 20000); lat = rng.uniform(south, north, 20000)
        keep = contains_xy(tolima, lon, lat)
        pts.extend(zip(lon[keep], lat[keep]))
    pts = pts[:50000]
    tiles = m162.dem_tiles()
    fwd = Transformer.from_crs("EPSG:4326", m162.CRS_PROJECTED, always_xy=True)
    inv = Transformer.from_crs(m162.CRS_PROJECTED, "EPSG:4326", always_xy=True)
    xs, ys = fwd.transform([p[0] for p in pts], [p[1] for p in pts])
    elevation = []
    for x, y in zip(xs, ys):
        sample = m162.dem_value_and_slope(x, y, tiles, inv)
        elevation.append(sample[0] if sample else np.nan)
    elevation = np.asarray(elevation)

    area = {}
    for year in sorted({min(s - 1, 2024) for s in SEASONS}):
        raster = m177.TiledRaster(RASTERS / f"colombia_coverage_{year}.tif")
        forest_edge, forest_low, forest_n, natural_n = [], [], 0, 0
        for (lon, lat), elev in zip(pts, elevation):
            w = fast_window(raster, lon, lat)
            centre = int(w[HALF, HALF])
            natural_n += centre in NATURAL
            if centre in FOREST:
                forest_n += 1
                fr = np.isin(w, list(FOREST)).mean(); ag = np.isin(w, list(AGRI)).mean()
                forest_edge.append(fr >= 0.10 and ag >= 0.10)
                forest_low.append(elev < 1000 if np.isfinite(elev) else np.nan)
        raster.handle.close()
        low = np.asarray([v for v in forest_low if v == v], dtype=float)
        area[year] = {"forest_points": forest_n, "edge_fraction_of_forest": float(np.mean(forest_edge)),
                      "below_1000_fraction_of_forest": float(low.mean()), "natural_fraction_of_tolima": natural_n / len(pts)}

    rows = list(csv.DictReader(COVER_ROWS.open(encoding="utf-8")))
    det_elev = {}
    for r in rows:
        if r["cover_group"] == "forest_formation":
            x, y = fwd.transform(float(r["longitude"]), float(r["latitude"]))
            sample = m162.dem_value_and_slope(x, y, tiles, inv)
            det_elev[(r["season"], r["acq_date"], r["latitude"], r["longitude"])] = sample[0] if sample else np.nan
    per_season = {}
    for s in SEASONS:
        sub = [r for r in rows if int(r["season"]) == s]
        forest = [r for r in sub if r["cover_group"] == "forest_formation"]
        edge = [float(r["window_fraction_forest_formation"]) >= 0.10 and float(r["window_fraction_agricultural_other"]) >= 0.10 for r in forest]
        elev = [det_elev[(r["season"], r["acq_date"], r["latitude"], r["longitude"])] for r in forest]
        low = [e < 1000 for e in elev if e == e]
        a = area[min(s - 1, 2024)]
        n_edge, n_int = sum(edge), len(edge) - sum(edge)
        n_low, n_high = sum(low), len(low) - sum(low)
        natural_share = sum(r["cover_group"] in ("forest_formation", "natural_non_forest") for r in sub) / len(sub)
        per_season[s] = {
            "detections": len(sub), "forest_detections": len(forest),
            "edge_rate_ratio": (n_edge / a["edge_fraction_of_forest"]) / (n_int / (1 - a["edge_fraction_of_forest"])) if n_int else None,
            "low_elevation_rate_ratio": (n_low / a["below_1000_fraction_of_forest"]) / (n_high / (1 - a["below_1000_fraction_of_forest"])) if n_high else None,
            "natural_selection_ratio": natural_share / a["natural_fraction_of_tolima"],
        }
    ref = [s for s in SEASONS if s != TARGET]

    def pos(metric):
        vals = [per_season[s][metric] for s in ref if per_season[s][metric] is not None]
        obs = per_season[TARGET][metric]
        return {"value_2026": round(obs, 3), "reference_min": round(min(vals), 3), "reference_median": round(float(np.median(vals)), 3),
                "reference_max": round(max(vals), 3), "percentile": round(100 * (sum(v < obs for v in vals) + 0.5 * sum(v == obs for v in vals)) / len(vals), 2)}

    det = [per_season[s]["detections"] for s in ref]
    nat = [per_season[s]["natural_selection_ratio"] for s in ref]
    no2019 = [i for i, s in enumerate(ref) if s != 2019]
    return {
        "area_denominators_by_map_year": {str(k): {kk: round(v, 4) if isinstance(v, float) else v for kk, v in d.items()} for k, d in area.items()},
        "per_season": {str(s): {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()} for s, d in per_season.items()},
        "position_2026": {m: pos(m) for m in ("edge_rate_ratio", "low_elevation_rate_ratio", "natural_selection_ratio")},
        "natural_selection_ratio_vs_detections": {
            "spearman_all": round(float(stats.spearmanr(det, nat).statistic), 4), "p_all": float(stats.spearmanr(det, nat).pvalue),
            "spearman_without_2019": round(float(stats.spearmanr([det[i] for i in no2019], [nat[i] for i in no2019]).statistic), 4),
            "p_without_2019": float(stats.spearmanr([det[i] for i in no2019], [nat[i] for i in no2019]).pvalue)},
    }


def part_m(plan: dict) -> dict:
    panel = pd.read_csv(PANEL)
    case_rows = panel[panel["case"] == 1].set_index("stratum")
    block_of = {s: (int(r.centre_easting_m // 10000), int(r.centre_northing_m // 10000)) for s, r in case_rows.iterrows()}
    panel["block"] = panel["stratum"].map(block_of)
    blocks = sorted(set(block_of.values()))

    def scaled(v):
        med = float(v.median()); q1, q3 = v.quantile([0.25, 0.75]); return (v.to_numpy(float) - med) / float(q3 - q1)

    specs = {"primary": ["elevation_m", "slope_degrees", "upra_frontier_fraction"],
             "cover_adjusted": ["elevation_m", "slope_degrees", "upra_frontier_fraction", "natural_cover_fraction"]}

    def fit(data, cols):
        exog = np.column_stack([scaled(data[c]) for c in cols])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = ConditionalLogit(data["case"].to_numpy(int), exog, groups=data["stratum_boot"].to_numpy(int)).fit(disp=False, maxiter=500)
        return np.exp(np.asarray(res.params))

    rng = np.random.default_rng(20261015)
    by_block = {b: panel[panel["block"] == b] for b in blocks}
    out = {"blocks": len(blocks), "strata": int(panel["stratum"].nunique())}
    base = panel.assign(stratum_boot=panel["stratum"])
    point = {k: fit(base, cols) for k, cols in specs.items()}
    draws = {k: [] for k in specs}
    failures = 0
    for _ in range(1000):
        chosen = rng.choice(len(blocks), size=len(blocks), replace=True)
        parts = []
        for j, b in enumerate(chosen):
            part = by_block[blocks[b]].copy()
            part["stratum_boot"] = part["stratum"] * 10000 + j
            parts.append(part)
        sample = pd.concat(parts, ignore_index=True)
        try:
            for k, cols in specs.items():
                draws[k].append(fit(sample, cols))
        except Exception:
            failures += 1
    for k, cols in specs.items():
        arr = np.asarray(draws[k])
        out[k] = {c: {"odds_ratio": round(float(point[k][i]), 3), "block_bootstrap_95": [round(float(np.quantile(arr[:, i], 0.025)), 3), round(float(np.quantile(arr[:, i], 0.975)), 3)]}
                  for i, c in enumerate(cols)}
    out["bootstrap_failures"] = failures
    out["replicates"] = 1000
    return out


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribe")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    audit = {"script": Path(__file__).name, "plan_sha256": sha256(PLAN), "status": "descriptivo y posterior",
             "K_missing_days_v2": part_k(plan), "L_area_denominators": part_l(plan), "M_spatial_dependence": part_m(plan),
             "inputs": [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(p)} for p in (DETECTIONS, COVER_ROWS, PANEL, GEOB, MUNICIPAL)]}
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=lambda o: o.item()) + "\n", encoding="utf-8")
    print(json.dumps({k: audit[k] for k in ("K_missing_days_v2", "M_spatial_dependence")}, ensure_ascii=False, indent=1, default=lambda o: o.item()))
    print(json.dumps({k: audit["L_area_denominators"][k] for k in ("position_2026", "natural_selection_ratio_vs_detections", "area_denominators_by_map_year")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
