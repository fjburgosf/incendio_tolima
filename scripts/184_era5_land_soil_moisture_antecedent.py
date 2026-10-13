"""Humedad del suelo antecedente de ERA5-Land para Tolima, mayo a julio de 1991 a 2026.

Sigue la parte H de `configs/review_robustness_plan_v2.json`, congelada antes de
descargar. Pide promedios mensuales de swvl1 y swvl2 en el recorte de la
climatología del script 138, conserva el GRIB crudo y promedia sobre los 198 centros
de Tolima con la regla del script 148. Descriptivo y posterior.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from collections import defaultdict
from pathlib import Path

import eccodes
import numpy as np
from scipy import stats


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "review_robustness_plan_v2.json"
RAW = ROOT / "data" / "raw" / "era5_land" / "monthly_soil_water_may_july_1991_2026.grib"
ENSO = ROOT / "data" / "interim" / "enso_indices_2012_2026_v1.csv"
ACTIVITY = ROOT / "data" / "processed" / "regional_fire_activity_department_season_v1.csv"
OUT = ROOT / "data" / "processed" / "tolima_soil_water_may_july_1991_2026_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_tolima_soil_water_antecedent_v1.json"
YEARS = list(range(1991, 2027))
BBOX = [6.2, -76.2, 2.0, -74.0]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def load(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def download() -> None:
    if RAW.exists():
        return
    import cdsapi
    m148 = load("148_build_august_fire_danger_climatology.py", "d148")
    credentials = {}
    for line in m148.ROOT.joinpath(".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            credentials[key.strip()] = value.strip().strip('"').strip("'")
    client = cdsapi.Client(url=credentials["CDS_API_URL"], key=credentials["CDS_API_KEY"], quiet=True, progress=False)
    partial = RAW.with_suffix(".grib.partial")
    RAW.parent.mkdir(parents=True, exist_ok=True)
    client.retrieve("reanalysis-era5-land-monthly-means", {
        "product_type": ["monthly_averaged_reanalysis"],
        "variable": ["volumetric_soil_water_layer_1", "volumetric_soil_water_layer_2"],
        "year": [str(y) for y in YEARS], "month": ["05", "06", "07"], "time": ["00:00"],
        "data_format": "grib", "download_format": "unarchived", "area": BBOX,
    }).download(str(partial))
    partial.replace(RAW)


def position(observed: float, reference: list[float]) -> dict:
    v = np.asarray(reference, float)
    below, equal = int(np.sum(v < observed)), int(np.sum(v == observed))
    med = float(np.median(v)); mad = float(np.median(np.abs(v - med)))
    return {"observed": round(observed, 5), "reference_median": round(med, 5), "rank_ascending": below + 1,
            "empirical_percentile": round(100 * (below + 0.5 * equal) / v.size, 2),
            "robust_z": round((observed - med) / (1.4826 * mad), 3) if mad > 0 else None}


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribe")
    download()
    m148 = load("148_build_august_fire_danger_climatology.py", "d148b")
    plan148 = json.loads(m148.PLAN_PATH.read_text(encoding="utf-8"))
    keys, weights = m148.load_grid_rule(plan148)
    fields: dict[tuple[str, int], list[np.ndarray]] = defaultdict(list)
    latitudes = longitudes = None
    with RAW.open("rb") as handle:
        while message := eccodes.codes_grib_new_from_file(handle):
            try:
                name = str(eccodes.codes_get(message, "shortName"))
                year = int(str(eccodes.codes_get(message, "dataDate"))[:4])
                values = np.asarray(eccodes.codes_get_array(message, "values"), dtype=float)
                if latitudes is None:
                    latitudes = np.asarray(eccodes.codes_get_array(message, "latitudes"), dtype=float)
                    longitudes = np.asarray(eccodes.codes_get_array(message, "longitudes"), dtype=float)
                fields[(name, year)].append(values)
            finally:
                eccodes.codes_release(message)
    indices = m148.retained_indices(latitudes, longitudes, keys)
    rows = []
    for year in YEARS:
        row = {"season": year}
        for name in ("swvl1", "swvl2"):
            months = fields[(name, year)]
            if len(months) != 3:
                raise RuntimeError(f"Se esperaban tres meses de {name} en {year} y hay {len(months)}")
            mean = np.mean(np.stack(months)[:, indices], axis=0)
            if not np.isfinite(mean).all():
                raise RuntimeError(f"Valores no finitos en {name} {year}")
            row[f"{name}_may_july_m3m3"] = float(np.average(mean, weights=weights))
        rows.append(row)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    by_year = {r["season"]: r for r in rows}
    oni = {int(r["season"]): float(r["oni_mjj"]) for r in csv.DictReader(ENSO.open(encoding="utf-8"))}
    detections = {int(r["season"]): float(r["detections"]) for r in csv.DictReader(ACTIVITY.open(encoding="utf-8-sig")) if r["department"] == "Tolima"}
    ref_short = list(range(2012, 2026))
    result = {}
    for name in ("swvl1", "swvl2"):
        col = f"{name}_may_july_m3m3"
        x = np.array([by_year[y][col] for y in ref_short]); d = np.log([detections[y] for y in ref_short])
        loo = []
        for i in range(len(ref_short)):
            mask = np.arange(len(ref_short)) != i
            b, a = np.polyfit(x[mask], d[mask], 1)
            loo.append(abs(a + b * x[i] - d[i]))
        b, a = np.polyfit(x, d, 1)
        result[name] = {
            "position_vs_1991_2025": position(by_year[2026][col], [by_year[y][col] for y in range(1991, 2026)]),
            "position_vs_2012_2025": position(by_year[2026][col], [by_year[y][col] for y in ref_short]),
            "spearman_with_oni_mjj_2012_2025": round(float(stats.spearmanr([oni[y] for y in ref_short], x).statistic), 4),
            "spearman_with_detections_2012_2025": round(float(stats.spearmanr(x, d).statistic), 4),
            "spearman_with_detections_p": float(stats.spearmanr(x, d).pvalue),
            "loo_mean_absolute_log_error": round(float(np.mean(loo)), 4),
            "prediction_2026": round(float(np.exp(a + b * by_year[2026][col])), 1),
            "reference_range_2012_2025": [round(float(x.min()), 5), round(float(x.max()), 5)],
        }
    audit = {"script": Path(__file__).name, "plan_sha256": sha256(PLAN), "status": "descriptivo y posterior",
             "raw": {"path": str(RAW.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(RAW)},
             "results": result, "output": str(OUT.relative_to(ROOT)).replace("\\", "/")}
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
