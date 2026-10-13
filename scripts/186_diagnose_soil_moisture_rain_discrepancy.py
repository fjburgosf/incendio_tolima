"""Diagnostica la discrepancia entre la humedad del suelo de ERA5-Land y la lluvia de CHIRPS en 2026.

El script 184 ubicó la humedad del suelo de mayo a julio de 2026 cerca de la mediana,
mientras que CHIRPS ubica la precipitación del mismo periodo en el percentil 7.1.
Este diagnóstico descarga la precipitación mensual de ERA5-Land para el mismo
recorte y compara las tres series en Tolima. No cambia ningún resultado previo.
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
RAW = ROOT / "data" / "raw" / "era5_land" / "monthly_total_precipitation_may_july_1991_2026.grib"
SOIL = ROOT / "data" / "processed" / "tolima_soil_water_may_july_1991_2026_v1.csv"
CHIRPS = ROOT / "data" / "processed" / "chirps_regional_antecedent_precipitation_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_soil_moisture_rain_discrepancy_v1.json"
YEARS = list(range(1991, 2027))
BBOX = [6.2, -76.2, 2.0, -74.0]
DAYS = {5: 31, 6: 30, 7: 31}


def load(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def percentile(observed, reference):
    v = np.asarray(reference, float)
    return round(100 * (np.sum(v < observed) + 0.5 * np.sum(v == observed)) / v.size, 2)


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribe")
    m148 = load("148_build_august_fire_danger_climatology.py", "d148")
    if not RAW.exists():
        import cdsapi
        creds = dict(line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines() if "=" in line)
        creds = {k.strip(): v.strip().strip('"').strip("'") for k, v in creds.items()}
        client = cdsapi.Client(url=creds["CDS_API_URL"], key=creds["CDS_API_KEY"], quiet=True, progress=False)
        partial = RAW.with_suffix(".grib.partial")
        client.retrieve("reanalysis-era5-land-monthly-means", {
            "product_type": ["monthly_averaged_reanalysis"], "variable": ["total_precipitation"],
            "year": [str(y) for y in YEARS], "month": ["05", "06", "07"], "time": ["00:00"],
            "data_format": "grib", "download_format": "unarchived", "area": BBOX}).download(str(partial))
        partial.replace(RAW)
    plan148 = json.loads(m148.PLAN_PATH.read_text(encoding="utf-8"))
    keys, weights = m148.load_grid_rule(plan148)
    months = defaultdict(dict)
    lat = lon = None
    units = set()
    with RAW.open("rb") as handle:
        while message := eccodes.codes_grib_new_from_file(handle):
            try:
                date = str(eccodes.codes_get(message, "dataDate"))
                values = np.asarray(eccodes.codes_get_array(message, "values"), float)
                units.add(str(eccodes.codes_get(message, "units")))
                if lat is None:
                    lat = np.asarray(eccodes.codes_get_array(message, "latitudes"), float)
                    lon = np.asarray(eccodes.codes_get_array(message, "longitudes"), float)
                months[int(date[:4])][int(date[4:6])] = values
            finally:
                eccodes.codes_release(message)
    idx = m148.retained_indices(lat, lon, keys)
    # Promedio mensual de ERA5-Land en metros por día, convertido a total del mes en milímetros.
    era5_rain = {y: float(sum(np.average(months[y][m][idx], weights=weights) * DAYS[m] * 1000 for m in (5, 6, 7))) for y in YEARS}
    soil = {int(r["season"]): float(r["swvl1_may_july_m3m3"]) for r in csv.DictReader(SOIL.open(encoding="utf-8"))}
    chirps = {int(r["season"]): float(r["chirps_may_july_mean_mm"]) for r in csv.DictReader(CHIRPS.open(encoding="utf-8")) if r["department"] == "Tolima"}
    ref = list(range(2012, 2026))
    audit = {
        "script": Path(__file__).name, "units": sorted(units),
        "era5_rain_may_july_mm": {str(y): round(era5_rain[y], 1) for y in range(2012, 2027)},
        "chirps_may_july_mm": {str(y): round(chirps[y], 1) for y in range(2012, 2027)},
        "percentile_2026": {
            "era5_rain_vs_1991_2025": percentile(era5_rain[2026], [era5_rain[y] for y in range(1991, 2026)]),
            "era5_rain_vs_2012_2025": percentile(era5_rain[2026], [era5_rain[y] for y in ref]),
            "chirps_vs_2012_2025": percentile(chirps[2026], [chirps[y] for y in ref]),
            "soil_swvl1_vs_2012_2025": percentile(soil[2026], [soil[y] for y in ref]),
        },
        "spearman_2012_2025": {
            "era5_rain_vs_chirps": round(float(stats.spearmanr([era5_rain[y] for y in ref], [chirps[y] for y in ref]).statistic), 4),
            "soil_vs_era5_rain": round(float(stats.spearmanr([soil[y] for y in ref], [era5_rain[y] for y in ref]).statistic), 4),
            "soil_vs_chirps": round(float(stats.spearmanr([soil[y] for y in ref], [chirps[y] for y in ref]).statistic), 4),
        },
        "raw": {"path": str(RAW.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(RAW)},
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
