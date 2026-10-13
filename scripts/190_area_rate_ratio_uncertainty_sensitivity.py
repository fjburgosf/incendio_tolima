"""Incertidumbre y sensibilidad de las razones de tasas por superficie del bosque.

Sigue `configs/review_robustness_plan_v4.json`, congelado antes de ejecutar. Reconstruye
la muestra de puntos del script 187, comprueba que reproduce sus razones y añade
conteos, intervalos exactos y de bootstrap de bloques, la definición de bosque por
ventana mayoritaria, umbrales alternativos de borde y altitud y una segunda muestra
Monte Carlo. Descriptivo y posterior.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
from pyproj import Transformer
from scipy import stats
from shapely import contains_xy
from shapely.geometry import shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "review_robustness_plan_v4.json"
COVER_ROWS = ROOT / "data" / "processed" / "tolima_detection_cover_v1.csv"
MUNICIPAL = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
RASTERS = ROOT / "data" / "raw" / "mapbiomas" / "coverage_collection3"
AUDIT_187 = ROOT / "results" / "exploratory" / "audit_review_v3_missing_days_area_rates_blocks_v1.json"
AUDIT = ROOT / "results" / "exploratory" / "audit_area_rate_ratio_uncertainty_sensitivity_v1.json"
SEASONS = list(range(2012, 2027))
REFERENCE = [s for s in SEASONS if s != 2026]
TARGET = 2026
FOREST = [3, 5, 6, 49]
AGRI = [14, 21, 35, 74]
HALF = 6
EDGE_THRESHOLDS = (0.05, 0.10, 0.20)
ELEVATION_THRESHOLDS = (800.0, 1000.0, 1200.0)


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


def sample_points(tolima, seed: int, n: int) -> list[tuple[float, float]]:
    rng = np.random.default_rng(seed)
    west, south, east, north = tolima.bounds
    pts = []
    while len(pts) < n:
        lon = rng.uniform(west, east, 20000); lat = rng.uniform(south, north, 20000)
        keep = contains_xy(tolima, lon, lat)
        pts.extend(zip(lon[keep], lat[keep]))
    return pts[:n]


def elevations(pts, m162, tiles, fwd, inv) -> np.ndarray:
    xs, ys = fwd.transform([p[0] for p in pts], [p[1] for p in pts])
    out = []
    for x, y in zip(xs, ys):
        s = m162.dem_value_and_slope(x, y, tiles, inv)
        out.append(s[0] if s else np.nan)
    return np.asarray(out, dtype=float)


def classify(pts, year: int, m177, m187) -> dict[str, np.ndarray]:
    raster = m177.TiledRaster(RASTERS / f"colombia_coverage_{year}.tif")
    centre, wf, wa = [], [], []
    for lon, lat in pts:
        w = m187.fast_window(raster, lon, lat)
        centre.append(int(w[HALF, HALF]) in FOREST)
        wf.append(float(np.isin(w, FOREST).mean())); wa.append(float(np.isin(w, AGRI).mean()))
    raster.handle.close()
    return {"centre_forest": np.asarray(centre), "wf": np.asarray(wf), "wa": np.asarray(wa)}


def ratio(n_in: float, n_out: float, p_in: float) -> float | None:
    if n_out == 0 or p_in in (0.0, 1.0):
        return None
    return (n_in / p_in) / (n_out / (1.0 - p_in))


def area_fractions(cls: dict, elev: np.ndarray, definition: str, edge_t: float, elev_t: float) -> dict:
    forest = cls["centre_forest"] if definition == "centre_pixel" else cls["wf"] >= 0.5
    edge = (cls["wf"] >= edge_t) & (cls["wa"] >= edge_t)
    finite = np.isfinite(elev)
    n_f = int(forest.sum()); n_fe = int((forest & finite).sum())
    p_edge = float(edge[forest].mean()); p_low = float((elev[forest & finite] < elev_t).mean())
    return {"forest_points": n_f, "forest_points_with_elevation": n_fe,
            "edge_fraction": p_edge, "edge_fraction_se": float(np.sqrt(p_edge * (1 - p_edge) / n_f)),
            "low_fraction": p_low, "low_fraction_se": float(np.sqrt(p_low * (1 - p_low) / n_fe))}


def detection_counts(rows, definition: str, edge_t: float, elev_t: float) -> dict:
    if definition == "centre_pixel":
        forest = [r for r in rows if r["cover_group"] == "forest_formation"]
    else:
        forest = [r for r in rows if r["wf"] >= 0.5]
    edge = [r["wf"] >= edge_t and r["wa"] >= edge_t for r in forest]
    low = [r["elev"] < elev_t for r in forest if np.isfinite(r["elev"])]
    return {"forest": len(forest), "edge": sum(edge), "interior": len(edge) - sum(edge), "low": sum(low), "high": len(low) - sum(low)}


def position(values: dict[int, float | None]) -> dict:
    obs = values[TARGET]
    ref = [values[s] for s in REFERENCE if values[s] is not None]
    return {"value_2026": None if obs is None else round(obs, 3), "reference_min": round(min(ref), 3), "reference_max": round(max(ref), 3),
            "reference_median": round(float(np.median(ref)), 3), "reference_n": len(ref),
            "percentile": None if obs is None else round(100 * (sum(v < obs for v in ref) + 0.5 * sum(v == obs for v in ref)) / len(ref), 2),
            "above_reference_max": None if obs is None else bool(obs > max(ref)),
            "inside_reference_range": None if obs is None else bool(min(ref) <= obs <= max(ref))}


def clopper_pearson_ratio(n_in: int, n_out: int, p_in: float) -> list[float]:
    n = n_in + n_out
    lo = stats.beta.ppf(0.025, n_in, n_out + 1) if n_in > 0 else 0.0
    hi = stats.beta.ppf(0.975, n_in + 1, n_out) if n_out > 0 else 1.0
    odds = lambda pi: np.inf if pi >= 1 else pi / (1 - pi) * (1 - p_in) / p_in
    return [round(float(odds(lo)), 3), round(float(odds(hi)), 3)]


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribe")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    m162 = load("162_build_spatial_case_control_panel.py", "s162")
    m177 = load("177_forest_cover_at_detections.py", "c177")
    m187 = load("187_review_v3_missing_days_area_rates_blocks.py", "r187")
    tolima = unary_union([shape(f["geometry"]) for f in json.loads(MUNICIPAL.read_text(encoding="utf-8"))["features"]])
    tiles = m162.dem_tiles()
    fwd = Transformer.from_crs("EPSG:4326", m162.CRS_PROJECTED, always_xy=True)
    inv = Transformer.from_crs(m162.CRS_PROJECTED, "EPSG:4326", always_xy=True)

    pts = sample_points(tolima, 20261014, 50000)
    elev = elevations(pts, m162, tiles, fwd, inv)
    years = sorted({min(s - 1, 2024) for s in SEASONS})
    cls = {y: classify(pts, y, m177, m187) for y in years}

    rows = list(csv.DictReader(COVER_ROWS.open(encoding="utf-8")))
    xs, ys = fwd.transform([float(r["longitude"]) for r in rows], [float(r["latitude"]) for r in rows])
    for r, x, y in zip(rows, xs, ys):
        s = m162.dem_value_and_slope(x, y, tiles, inv)
        r["elev"] = s[0] if s else np.nan
        r["wf"] = float(r["window_fraction_forest_formation"]); r["wa"] = float(r["window_fraction_agricultural_other"])
        r["x"], r["y"] = x, y
    by_season = {s: [r for r in rows if int(r["season"]) == s] for s in SEASONS}

    def series(definition: str, edge_t: float, elev_t: float) -> dict:
        out = {}
        for s in SEASONS:
            a = area_fractions(cls[min(s - 1, 2024)], elev, definition, edge_t, elev_t)
            c = detection_counts(by_season[s], definition, edge_t, elev_t)
            out[s] = {"counts": c, "area": a,
                      "low_elevation_rate_ratio": ratio(c["low"], c["high"], a["low_fraction"]),
                      "edge_rate_ratio": ratio(c["edge"], c["interior"], a["edge_fraction"])}
        return out

    primary = series("centre_pixel", 0.10, 1000.0)
    prior = json.loads(AUDIT_187.read_text(encoding="utf-8"))["L_area_denominators"]["per_season"]
    diffs = [abs(primary[s][m] - prior[str(s)][m]) for s in SEASONS for m in ("low_elevation_rate_ratio", "edge_rate_ratio") if primary[s][m] is not None]
    reproduction = max(diffs)
    if reproduction > 1e-3:
        raise RuntimeError(f"No se reproduce el script 187, diferencia máxima {reproduction}")

    p_counts = {str(s): {"counts": d["counts"], "area": {k: round(v, 4) if isinstance(v, float) else v for k, v in d["area"].items()}}
                for s, d in primary.items()}

    c26, a26 = primary[TARGET]["counts"], primary[TARGET]["area"]
    exact = {"low_elevation_rate_ratio": clopper_pearson_ratio(c26["low"], c26["high"], a26["low_fraction"]),
             "edge_rate_ratio": clopper_pearson_ratio(c26["edge"], c26["interior"], a26["edge_fraction"])}
    forest26 = [r for r in by_season[TARGET] if r["cover_group"] == "forest_formation"]
    blocks = {}
    for r in forest26:
        blocks.setdefault((int(r["x"] // 10000), int(r["y"] // 10000)), []).append(r)
    keys = sorted(blocks)
    rng = np.random.default_rng(20261016)
    draws = {"low_elevation_rate_ratio": [], "edge_rate_ratio": []}
    for _ in range(2000):
        chosen = [r for k in rng.choice(len(keys), size=len(keys), replace=True) for r in blocks[keys[k]]]
        low = [r["elev"] < 1000 for r in chosen if np.isfinite(r["elev"])]
        edge = [r["wf"] >= 0.10 and r["wa"] >= 0.10 for r in chosen]
        p_low = rng.binomial(a26["forest_points_with_elevation"], a26["low_fraction"]) / a26["forest_points_with_elevation"]
        p_edge = rng.binomial(a26["forest_points"], a26["edge_fraction"]) / a26["forest_points"]
        rl = ratio(sum(low), len(low) - sum(low), p_low); re = ratio(sum(edge), len(edge) - sum(edge), p_edge)
        draws["low_elevation_rate_ratio"].append(np.inf if rl is None else rl)
        draws["edge_rate_ratio"].append(np.inf if re is None else re)
    boot = {}
    for m, v in draws.items():
        arr = np.asarray(v)
        boot[m] = {"interval_95": [round(float(np.quantile(arr, 0.025)), 3), round(float(np.quantile(arr, 0.975)), 3)],
                   "share_infinite": round(float(np.mean(~np.isfinite(arr))), 4)}
    hist_max_low = position({s: primary[s]["low_elevation_rate_ratio"] for s in SEASONS})["reference_max"]

    sens = {}
    for definition in ("centre_pixel", "window_majority"):
        for et in ELEVATION_THRESHOLDS:
            ser = series(definition, 0.10, et)
            sens[f"low_elevation|{definition}|{int(et)}m"] = {**position({s: ser[s]["low_elevation_rate_ratio"] for s in SEASONS}),
                                                             "counts_2026": ser[TARGET]["counts"], "low_fraction_2026": round(ser[TARGET]["area"]["low_fraction"], 4)}
        for ed in EDGE_THRESHOLDS:
            ser = series(definition, ed, 1000.0)
            sens[f"edge|{definition}|{int(ed * 100)}pct"] = {**position({s: ser[s]["edge_rate_ratio"] for s in SEASONS}),
                                                            "counts_2026": ser[TARGET]["counts"], "edge_fraction_2026": round(ser[TARGET]["area"]["edge_fraction"], 4)}

    pts2 = sample_points(tolima, 20261017, 100000)
    elev2 = elevations(pts2, m162, tiles, fwd, inv)
    a2 = area_fractions(classify(pts2, 2024, m177, m187), elev2, "centre_pixel", 0.10, 1000.0)
    mc = {"sample_1_seed_20261014_n50000": {"low_fraction": round(a26["low_fraction"], 4), "edge_fraction": round(a26["edge_fraction"], 4),
                                             "low_elevation_rate_ratio": round(primary[TARGET]["low_elevation_rate_ratio"], 3), "edge_rate_ratio": round(primary[TARGET]["edge_rate_ratio"], 3)},
          "sample_2_seed_20261017_n100000": {"low_fraction": round(a2["low_fraction"], 4), "edge_fraction": round(a2["edge_fraction"], 4),
                                              "low_elevation_rate_ratio": round(ratio(c26["low"], c26["high"], a2["low_fraction"]), 3),
                                              "edge_rate_ratio": round(ratio(c26["edge"], c26["interior"], a2["edge_fraction"]), 3)}}

    low_keys = [k for k in sens if k.startswith("low_elevation")]
    edge_keys = [k for k in sens if k.startswith("edge|")]
    rule = plan["reporting_rule"]
    verdict = {
        "low_elevation_all_sensitivities_above_reference_max": all(sens[k]["above_reference_max"] for k in low_keys),
        "low_elevation_failing_sensitivities": [k for k in low_keys if not sens[k]["above_reference_max"]],
        "low_elevation_bootstrap_lower_above_reference_max": bool(boot["low_elevation_rate_ratio"]["interval_95"][0] > hist_max_low),
        "edge_inside_reference_range_primary_definition": [sens[f"edge|centre_pixel|{int(e * 100)}pct"]["inside_reference_range"] for e in EDGE_THRESHOLDS],
        "rule_low": rule["statement_low_elevation"], "rule_edge": rule["statement_edge"],
    }
    audit = {"script": Path(__file__).name, "plan_sha256": sha256(PLAN), "status": "descriptivo y posterior",
             "reproduction_of_187_max_abs_difference": reproduction,
             "P_counts_by_season": p_counts,
             "Q_uncertainty_2026": {"counts": c26, "exact_conditional_95": exact, "block_bootstrap": {**boot, "blocks": len(keys), "replicates": 2000},
                                     "reference_max_low_elevation": hist_max_low},
             "R_S_sensitivities": sens, "T_monte_carlo_stability": mc, "verdict": verdict,
             "non_vegetated_2026": sum(r["cover_group"] == "non_vegetated" for r in by_season[TARGET]),
             "inputs": [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(p)} for p in (COVER_ROWS, MUNICIPAL, AUDIT_187)]}
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=lambda o: o.item()) + "\n", encoding="utf-8")
    print(json.dumps({k: audit[k] for k in ("reproduction_of_187_max_abs_difference", "Q_uncertainty_2026", "T_monte_carlo_stability", "verdict")}, ensure_ascii=False, indent=1, default=lambda o: o.item()))
    for k, v in sens.items():
        print(k, v)


if __name__ == "__main__":
    main()
