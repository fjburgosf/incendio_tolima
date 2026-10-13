"""Corroboración Sentinel-2 versión 2, con fechas explícitas, huellas por detección y referencia forestal.

Sigue la parte N de `configs/review_robustness_plan_v3.json`, congelada antes de
ejecutar. Corrige tres limitaciones de la versión 1 (script 181) señaladas en la
tercera revisión simulada. Las fechas se eligen con la API de catálogo y se registran,
la huella es la unión de cuadros de 375 m centrados en cada detección de bosque y la
referencia usa solo píxeles de bosque a más de 500 m de toda detección de la celda.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import math
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import tifffile


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "review_robustness_plan_v3.json"
DETECTIONS = ROOT / "data" / "interim" / "regional_fire_detections_labelled_v1.csv"
COVER_ROWS = ROOT / "data" / "processed" / "tolima_detection_cover_v1.csv"
MAPBIOMAS_2024 = ROOT / "data" / "raw" / "mapbiomas" / "coverage_collection3" / "colombia_coverage_2024.tif"
RAW = ROOT / "data" / "raw" / "sentinel2_forest_cells_2026_v2"
CELLS_OUT = ROOT / "results" / "exploratory" / "sentinel2_forest_cell_corroboration_v2.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_sentinel2_forest_corroboration_v2.json"
CATALOG_URL = "https://sh.dataspace.copernicus.eu/api/v1/catalog/1.0.0/search"
FOREST_CODES = {3, 5, 6, 49}
USABLE_SCL = {4, 5, 6, 7}
PIXEL_M = 20.0
HALF_BOX_M = 1000.0
HALF_FOOTPRINT_M = 187.5
REFERENCE_MIN_M = 500.0


def load(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def cells() -> list[dict]:
    forest_keys = set()
    for r in csv.DictReader(COVER_ROWS.open(encoding="utf-8")):
        if r["season"] == "2026" and r["cover_group"] == "forest_formation":
            forest_keys.add((r["acq_date"], round(float(r["latitude"]), 5), round(float(r["longitude"]), 5)))
    forest, last_any = defaultdict(list), defaultdict(str)
    for r in csv.DictReader(DETECTIONS.open(encoding="utf-8-sig")):
        if r["season"] != "2026" or r["department"] != "Tolima":
            continue
        cell = (int(r["cell_x"]), int(r["cell_y"]))
        last_any[cell] = max(last_any[cell], r["acq_date"])
        key = (r["acq_date"], round(float(r["latitude"]), 5), round(float(r["longitude"]), 5))
        if key in forest_keys:
            forest[cell].append((r["acq_date"], float(r["latitude"]), float(r["longitude"])))
    if sum(len(v) for v in forest.values()) != len(forest_keys):
        raise RuntimeError("No se enlazaron todas las detecciones de bosque")
    return [{"cell": c, "points": [(p[1], p[2]) for p in pts], "first": min(p[0] for p in pts), "last": last_any[c]}
            for c, pts in sorted(forest.items())]


def catalog_best(token: str, bbox: list[float], start: date, end: date) -> tuple[str | None, float | None, int]:
    if end < start:
        return None, None, 0
    body = {"bbox": bbox, "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
            "collections": ["sentinel-2-l2a"], "limit": 100}
    request = urllib.request.Request(CATALOG_URL, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        features = json.loads(response.read().decode())["features"]
    if not features:
        return None, None, 0
    best = min(features, key=lambda f: (f["properties"].get("eo:cloud_cover", 100.0), f["properties"]["datetime"]))
    return best["properties"]["datetime"][:10], float(best["properties"].get("eo:cloud_cover", float("nan"))), len(features)


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribe")
    helper = load("79_validate_largest_gwis_event_with_sentinel2_nbr.py", "s2h")
    m177 = load("177_forest_cover_at_detections.py", "c177")
    mapbiomas = m177.TiledRaster(MAPBIOMAS_2024)
    token = helper.access_token(helper.read_env(helper.ENV_FILE))
    RAW.mkdir(parents=True, exist_ok=True)
    size = int(round(2 * HALF_BOX_M / PIXEL_M))
    offsets = (np.arange(size) + 0.5) * PIXEL_M - HALF_BOX_M
    dx_m, dy_m = np.meshgrid(offsets, -offsets)
    rows, raw_files = [], []
    for i, c in enumerate(cells()):
        lat0 = float(np.mean([p[0] for p in c["points"]])); lon0 = float(np.mean([p[1] for p in c["points"]]))
        mlat = 110574.0; mlon = 111320.0 * math.cos(math.radians(lat0))
        bbox = [lon0 - HALF_BOX_M / mlon, lat0 - HALF_BOX_M / mlat, lon0 + HALF_BOX_M / mlon, lat0 + HALF_BOX_M / mlat]
        first, last = date.fromisoformat(c["first"]), date.fromisoformat(c["last"])
        pre_date, pre_cc, pre_n = catalog_best(token, bbox, date(2026, 7, 1), first - timedelta(days=1))
        post_date, post_cc, post_n = catalog_best(token, bbox, last + timedelta(days=1), date(2026, 9, 30))
        record = {"cell_x": c["cell"][0], "cell_y": c["cell"][1], "forest_detections": len(c["points"]), "first_detection": c["first"],
                  "last_detection_in_cell": c["last"], "pre_date": pre_date, "pre_cloud_cover": pre_cc, "post_date": post_date,
                  "post_cloud_cover": post_cc, "days_between": (date.fromisoformat(post_date) - date.fromisoformat(pre_date)).days if pre_date and post_date else None}
        if not (pre_date and post_date):
            rows.append({**record, "evaluable": False, "corroborated": False, "status": "not_evaluable", "reason": "sin escena"})
            continue
        arrays = {}
        for label, day in (("pre", pre_date), ("post", post_date)):
            path = RAW / f"cell_{c['cell'][0]}_{c['cell'][1]}_{label}_{day}.tif"
            if not path.exists():
                d = date.fromisoformat(day)
                body = helper.process_body(bbox, size, size, d)
                payload, _ = helper.download(token, body)
                path.write_bytes(payload)
            raw_files.append({"path": str(path.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(path)})
            arrays[label] = tifffile.imread(path).astype(float)
        pre, post = arrays["pre"], arrays["post"]
        usable = (np.isin(pre[..., 2], list(USABLE_SCL)) & (pre[..., 3] == 1) & np.isin(post[..., 2], list(USABLE_SCL)) & (post[..., 3] == 1))
        px_lon = lon0 + dx_m / mlon; px_lat = lat0 + dy_m / mlat
        forest = np.zeros(usable.shape, bool)
        for r in range(size):
            for col in range(size):
                forest[r, col] = int(mapbiomas.window(float(px_lon[r, col]), float(px_lat[r, col]), 0)[0, 0]) in FOREST_CODES
        footprint = np.zeros(usable.shape, bool)
        min_dist = np.full(usable.shape, np.inf)
        for plat, plon in c["points"]:
            ex = (plon - lon0) * mlon; ey = (plat - lat0) * mlat
            footprint |= (np.abs(dx_m - ex) <= HALF_FOOTPRINT_M) & (np.abs(dy_m - ey) <= HALF_FOOTPRINT_M)
            min_dist = np.minimum(min_dist, np.hypot(dx_m - ex, dy_m - ey))
        reference = forest & (min_dist > REFERENCE_MIN_M)
        with np.errstate(divide="ignore", invalid="ignore"):
            dnbr = (pre[..., 0] - pre[..., 1]) / (pre[..., 0] + pre[..., 1]) - (post[..., 0] - post[..., 1]) / (post[..., 0] + post[..., 1])
        fp_usable = float(usable[footprint].mean())
        fp_forest = usable & footprint & forest & np.isfinite(dnbr)
        ref = usable & reference & np.isfinite(dnbr)
        evaluable = fp_usable >= 0.80 and int(fp_forest.sum()) >= 10 and int(ref.sum()) >= 30
        fp_med = float(np.median(dnbr[fp_forest])) if fp_forest.any() else float("nan")
        ref_med = float(np.median(dnbr[ref])) if ref.any() else float("nan")
        corroborated = bool(evaluable and fp_med >= 0.10 and fp_med - ref_med >= 0.10)
        rows.append({**record, "footprint_usable_fraction": round(fp_usable, 4), "footprint_forest_usable": int(fp_forest.sum()),
                     "reference_forest_usable": int(ref.sum()), "dnbr_forest_footprint_median": round(fp_med, 4) if np.isfinite(fp_med) else "",
                     "dnbr_forest_reference_median": round(ref_med, 4) if np.isfinite(ref_med) else "", "evaluable": evaluable,
                     "corroborated": corroborated, "status": "corroborated" if corroborated else ("not_corroborated" if evaluable else "not_evaluable"),
                     "reason": ""})
        print(i + 1, rows[-1]["status"], pre_date, post_date, rows[-1].get("dnbr_forest_footprint_median"), rows[-1].get("dnbr_forest_reference_median"), flush=True)

    fields = sorted({k for r in rows for k in r}, key=lambda k: list(rows[-1]).index(k) if k in rows[-1] else 99)
    with CELLS_OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    ev = [r for r in rows if r["evaluable"]]
    pre_dates = sorted(r["pre_date"] for r in rows if r.get("pre_date"))
    audit = {
        "script": Path(__file__).name, "plan_sha256": sha256(PLAN), "status": "descriptivo y posterior",
        "cells": len(rows), "forest_detections": sum(r["forest_detections"] for r in rows),
        "status_counts": {s: sum(r["status"] == s for r in rows) for s in ("corroborated", "not_corroborated", "not_evaluable")},
        "corroborated_detections": sum(r["forest_detections"] for r in rows if r["corroborated"]),
        "pre_dates_range": [pre_dates[0], pre_dates[-1]] if pre_dates else None,
        "pre_dates_in_july_fraction": round(float(np.mean([d.startswith("2026-07") for d in pre_dates])), 4) if pre_dates else None,
        "days_between_median_and_range": [float(np.median([r["days_between"] for r in ev])), min(r["days_between"] for r in ev), max(r["days_between"] for r in ev)] if ev else None,
        "evaluable_median_footprint_dnbr": round(float(np.median([r["dnbr_forest_footprint_median"] for r in ev])), 4) if ev else None,
        "evaluable_median_reference_dnbr": round(float(np.median([r["dnbr_forest_reference_median"] for r in ev])), 4) if ev else None,
        "raw_files": raw_files, "output": str(CELLS_OUT.relative_to(ROOT)).replace("\\", "/"),
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in audit.items() if k != "raw_files"}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
