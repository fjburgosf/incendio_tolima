"""Corrobora con Sentinel-2 las celdas de 2026 con detecciones VIIRS en píxeles de bosque.

Sigue la parte B de `configs/forest_validation_plan_v1.json`, congelada antes de
descargar imágenes de las celdas evaluadas. Reutiliza la autenticación y la petición
a la API Process de Copernicus Data Space Ecosystem del script 79 y el lector de
MapBiomas del script 177. Las imágenes crudas se guardan sin modificar y se
reutilizan si ya existen. Descriptivo y posterior.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import math
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import tifffile


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "forest_validation_plan_v1.json"
DETECTIONS = ROOT / "data" / "interim" / "regional_fire_detections_labelled_v1.csv"
COVER_ROWS = ROOT / "data" / "processed" / "tolima_detection_cover_v1.csv"
MAPBIOMAS_2024 = ROOT / "data" / "raw" / "mapbiomas" / "coverage_collection3" / "colombia_coverage_2024.tif"
RAW = ROOT / "data" / "raw" / "sentinel2_forest_cells_2026"
CELLS_OUT = ROOT / "results" / "exploratory" / "sentinel2_forest_cell_corroboration_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_sentinel2_forest_corroboration_v1.json"
FOREST_CODES = {3, 5, 6, 49}
USABLE_SCL = {4, 5, 6, 7}
PIXEL_M = 20.0
HALF_BOX_M = 750.0
HALF_FOOTPRINT_M = 187.5
REFERENCE_MIN_M = 500.0


def load(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def cells_2026() -> list[dict]:
    forest_keys = set()
    with COVER_ROWS.open(encoding="utf-8") as handle:
        for r in csv.DictReader(handle):
            if r["season"] == "2026" and r["cover_group"] == "forest_formation":
                forest_keys.add((r["acq_date"], round(float(r["latitude"]), 5), round(float(r["longitude"]), 5)))
    forest = defaultdict(list)
    last_any = defaultdict(str)
    with DETECTIONS.open(encoding="utf-8-sig", newline="") as handle:
        for r in csv.DictReader(handle):
            if r["season"] != "2026" or r["department"] != "Tolima":
                continue
            cell = (int(r["cell_x"]), int(r["cell_y"]))
            last_any[cell] = max(last_any[cell], r["acq_date"])
            key = (r["acq_date"], round(float(r["latitude"]), 5), round(float(r["longitude"]), 5))
            if key in forest_keys:
                forest[cell].append((r["acq_date"], float(r["latitude"]), float(r["longitude"])))
    matched = sum(len(v) for v in forest.values())
    if matched != len(forest_keys):
        raise RuntimeError(f"Se esperaban {len(forest_keys)} detecciones de bosque y se enlazaron {matched}")
    out = []
    for cell, rows in sorted(forest.items()):
        out.append({"cell_x": cell[0], "cell_y": cell[1], "forest_detections": len(rows),
                    "latitude": float(np.mean([r[1] for r in rows])), "longitude": float(np.mean([r[2] for r in rows])),
                    "first_forest_detection": min(r[0] for r in rows), "last_detection_in_cell": last_any[cell]})
    return out


def request(helper, token, bbox, size, start: date, end: date) -> np.ndarray:
    body = helper.process_body(bbox, size, size, start)
    body["input"]["data"][0]["dataFilter"]["timeRange"] = {"from": f"{start.isoformat()}T00:00:00Z", "to": f"{end.isoformat()}T23:59:59Z"}
    payload, _ = helper.download(token, body)
    return payload


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribe")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))["B_sentinel2_corroboration"]
    helper = load("79_validate_largest_gwis_event_with_sentinel2_nbr.py", "s2helper")
    m177 = load("177_forest_cover_at_detections.py", "cover177")
    mapbiomas = m177.TiledRaster(MAPBIOMAS_2024)
    token = helper.access_token(helper.read_env(helper.ENV_FILE))
    RAW.mkdir(parents=True, exist_ok=True)
    size = int(round(2 * HALF_BOX_M / PIXEL_M))
    offsets = (np.arange(size) + 0.5) * PIXEL_M - HALF_BOX_M
    dx_m, dy_m = np.meshgrid(offsets, -offsets)
    footprint = (np.abs(dx_m) <= HALF_FOOTPRINT_M) & (np.abs(dy_m) <= HALF_FOOTPRINT_M)
    reference = np.hypot(dx_m, dy_m) > REFERENCE_MIN_M

    rows, raw_files = [], []
    for i, cell in enumerate(cells_2026()):
        lat, lon = cell["latitude"], cell["longitude"]
        dlat = HALF_BOX_M / 110574.0
        dlon = HALF_BOX_M / (111320.0 * math.cos(math.radians(lat)))
        bbox = [lon - dlon, lat - dlat, lon + dlon, lat + dlat]
        first = date.fromisoformat(cell["first_forest_detection"])
        last = date.fromisoformat(cell["last_detection_in_cell"])
        windows = {"pre": (date(2026, 7, 1), first - timedelta(days=1)), "post": (last + timedelta(days=1), date(2026, 9, 30))}
        arrays = {}
        for label, (start, end) in windows.items():
            path = RAW / f"cell_{cell['cell_x']}_{cell['cell_y']}_{label}.tif"
            if not path.exists():
                path.write_bytes(request(helper, token, bbox, size, start, end))
            raw_files.append({"path": str(path.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(path)})
            arrays[label] = tifffile.imread(path).astype(float)
        pre, post = arrays["pre"], arrays["post"]
        usable = (np.isin(pre[..., 2], list(USABLE_SCL)) & (pre[..., 3] == 1)
                  & np.isin(post[..., 2], list(USABLE_SCL)) & (post[..., 3] == 1))
        lon_px = lon + dx_m / (111320.0 * math.cos(math.radians(lat)))
        lat_px = lat + dy_m / 110574.0
        forest = np.zeros_like(usable)
        for r in range(size):
            for c in range(size):
                forest[r, c] = int(mapbiomas.window(float(lon_px[r, c]), float(lat_px[r, c]), 0)[0, 0]) in FOREST_CODES
        with np.errstate(divide="ignore", invalid="ignore"):
            nbr_pre = (pre[..., 0] - pre[..., 1]) / (pre[..., 0] + pre[..., 1])
            nbr_post = (post[..., 0] - post[..., 1]) / (post[..., 0] + post[..., 1])
        dnbr = nbr_pre - nbr_post
        fp_usable_fraction = float(usable[footprint].mean())
        fp_forest = usable & footprint & forest & np.isfinite(dnbr)
        ref = usable & reference & np.isfinite(dnbr)
        evaluable = fp_usable_fraction >= 0.80 and int(fp_forest.sum()) >= 10 and int(ref.sum()) > 0
        fp_median = float(np.median(dnbr[fp_forest])) if fp_forest.any() else float("nan")
        ref_median = float(np.median(dnbr[ref])) if ref.any() else float("nan")
        corroborated = bool(evaluable and fp_median >= 0.10 and fp_median - ref_median >= 0.10)
        rows.append({**cell, "footprint_usable_fraction": round(fp_usable_fraction, 4), "footprint_usable_forest_pixels": int(fp_forest.sum()),
                     "footprint_forest_pixels_total": int((footprint & forest).sum()), "reference_usable_pixels": int(ref.sum()),
                     "dnbr_forest_footprint_median": round(fp_median, 4) if np.isfinite(fp_median) else "",
                     "dnbr_reference_median": round(ref_median, 4) if np.isfinite(ref_median) else "",
                     "evaluable": evaluable, "corroborated": corroborated,
                     "status": "corroborated" if corroborated else ("not_corroborated" if evaluable else "not_evaluable")})
        print(i + 1, rows[-1]["status"], rows[-1]["footprint_usable_fraction"], rows[-1]["dnbr_forest_footprint_median"], flush=True)

    CELLS_OUT.parent.mkdir(parents=True, exist_ok=True)
    with CELLS_OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    status = {s: sum(r["status"] == s for r in rows) for s in ("corroborated", "not_corroborated", "not_evaluable")}
    evaluable_rows = [r for r in rows if r["evaluable"]]
    audit = {
        "script": Path(__file__).name,
        "plan_sha256": sha256(PLAN),
        "status_note": "descriptivo y posterior",
        "cells_evaluated": len(rows),
        "forest_detections_covered": sum(r["forest_detections"] for r in rows),
        "status_counts": status,
        "corroborated_fraction_of_evaluable": round(status["corroborated"] / len(evaluable_rows), 4) if evaluable_rows else None,
        "evaluable_dnbr_footprint_median_of_medians": round(float(np.median([r["dnbr_forest_footprint_median"] for r in evaluable_rows])), 4) if evaluable_rows else None,
        "rule": plan["corroboration_rule"],
        "inputs": [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(p)} for p in (DETECTIONS, COVER_ROWS, MAPBIOMAS_2024)],
        "raw_files": raw_files,
        "outputs": [str(CELLS_OUT.relative_to(ROOT)).replace("\\", "/")],
        "limits": plan["limits"],
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: audit[k] for k in ("cells_evaluated", "forest_detections_covered", "status_counts",
                                            "corroborated_fraction_of_evaluable", "evaluable_dnbr_footprint_median_of_medians")}, indent=2))


if __name__ == "__main__":
    main()
