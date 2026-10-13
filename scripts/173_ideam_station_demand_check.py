"""Contrasta la posición de agosto de 2026 en demanda atmosférica con estaciones IDEAM.

Sigue la parte B de `configs/fire_location_demand_plan_v1.json` y su enmienda
técnica `configs/fire_location_demand_plan_v1_ideam_amendment.json`, ambas escritas
antes de descargar las series. Descarga por API pública la temperatura y la
humedad horarias de las estaciones de Tolima con datos en agosto de 2026, en la
ventana del 12 al 31 de agosto de 2012 a 2026, y aplica una prueba de ciclo
diario a la humedad antes de calcular el déficit de presión de vapor.
Análisis descriptivo y posterior.
"""

from __future__ import annotations

import csv
import hashlib
import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "fire_location_demand_plan_v1.json"
AMENDMENT = ROOT / "configs" / "fire_location_demand_plan_v1_ideam_amendment.json"
RAW = ROOT / "data" / "raw" / "ideam_hourly_temperature_humidity_2026-10-12"
GRID = ROOT / "data" / "interim" / "era5_land_august_hourly_vpd_grid_1991_2026_v1.npz"
STATION_OUT = ROOT / "results" / "exploratory" / "ideam_station_demand_positions_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_ideam_station_demand_v1.json"
DATASETS = {"temperature": "sbwg-7ju4", "humidity": "uext-mhny"}
YEARS = list(range(2012, 2027))
TARGET = 2026
FIRST_DAY, LAST_DAY = 12, 31
WINDOW_HOURS = (LAST_DAY - FIRST_DAY + 1) * 24
HEADERS = {"User-Agent": "reproducible-audit/1.0"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def soql(dataset: str, params: dict) -> list[dict]:
    url = f"https://www.datos.gov.co/resource/{dataset}.json?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=300) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            if attempt == 3:
                raise
            time.sleep(10 * (attempt + 1))
    return []


def fetch_station(dataset: str, code: str) -> list[dict]:
    where = (f"codigoestacion='{code}' AND date_extract_m(fechaobservacion)=8 AND date_extract_d(fechaobservacion)>={FIRST_DAY} "
             f"AND date_extract_y(fechaobservacion)>={YEARS[0]} AND date_extract_y(fechaobservacion)<={YEARS[-1]}")
    rows, offset = [], 0
    while True:
        page = soql(dataset, {"$select": "codigoestacion,codigosensor,descripcionsensor,fechaobservacion,valorobservado,unidadmedida",
                              "$where": where, "$order": "fechaobservacion,codigosensor", "$limit": 50000, "$offset": offset})
        rows.extend(page)
        if len(page) < 50000:
            return rows
        offset += 50000


def vpd(temperature_c: np.ndarray, humidity_pct: np.ndarray) -> np.ndarray:
    saturation = 0.6108 * np.exp((17.27 * temperature_c) / (temperature_c + 237.3))
    return saturation * (1.0 - humidity_pct / 100.0)


def percentile(observed: float, reference: list[float]) -> float:
    values = np.asarray(reference, float)
    return round(100.0 * (np.sum(values < observed) + 0.5 * np.sum(values == observed)) / values.size, 2)


def main() -> None:
    if AUDIT.exists():
        raise FileExistsError("La auditoría ya existe y no se sobrescribe")
    RAW.mkdir(parents=True, exist_ok=True)

    stations = {}
    for variable, dataset in DATASETS.items():
        listing = soql(dataset, {"$select": "codigoestacion,nombreestacion,municipio,latitud,longitud,count(*) as n",
                                 "$where": "departamento in ('Tolima','TOLIMA') AND fechaobservacion between '2026-08-12T00:00:00' and '2026-08-31T23:59:59'",
                                 "$group": "codigoestacion,nombreestacion,municipio,latitud,longitud", "$limit": 1000})
        for row in listing:
            stations.setdefault(row["codigoestacion"], {"name": row["nombreestacion"], "municipality": row["municipio"],
                                                        "latitude": float(row["latitud"]), "longitude": float(row["longitud"]), "variables": set()})
            stations[row["codigoestacion"]]["variables"].add(variable)
    stations = {k: v for k, v in stations.items() if v["variables"] == set(DATASETS)}

    raw_files = []
    hourly: dict[str, dict[str, dict[datetime, list[float]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    descriptions: dict[str, set] = defaultdict(set)
    out_of_range = defaultdict(int)
    for code in sorted(stations):
        for variable, dataset in DATASETS.items():
            path = RAW / f"{variable}_{code}.json"
            if not path.exists():
                path.write_text(json.dumps(fetch_station(dataset, code), ensure_ascii=False), encoding="utf-8")
            raw_files.append({"path": str(path.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(path)})
            for row in json.loads(path.read_text(encoding="utf-8")):
                stamp = datetime.fromisoformat(row["fechaobservacion"][:19]).replace(minute=0, second=0)
                value = float(row["valorobservado"])
                descriptions[variable].add((row["codigosensor"], row["descripcionsensor"], stamp.year >= 2024))
                valid = (-5 <= value <= 45) if variable == "temperature" else (0 < value <= 100)
                if not valid:
                    out_of_range[variable] += 1
                    continue
                hourly[code][variable][stamp].append(value)

    grid = np.load(GRID)
    grid_seasons = [int(s) for s in grid["seasons"]]
    grid_lat, grid_lon = grid["latitudes"], grid["longitudes"]
    grid_vpd = grid["vpd_kpa"]

    station_rows, gate_log, time_check = [], {}, {}
    for code, meta in sorted(stations.items()):
        nearest = int(np.argmin((grid_lat - meta["latitude"]) ** 2 + (grid_lon - meta["longitude"]) ** 2))
        series_by_year = {}
        for year in YEARS:
            start = datetime(year, 8, FIRST_DAY)
            hours = [start + timedelta(hours=h) for h in range(WINDOW_HOURS)]
            t = np.array([np.mean(hourly[code]["temperature"][h]) if hourly[code]["temperature"].get(h) else np.nan for h in hours])
            rh = np.array([np.mean(hourly[code]["humidity"][h]) if hourly[code]["humidity"].get(h) else np.nan for h in hours])
            paired = np.isfinite(t) & np.isfinite(rh)
            if paired.sum() < 0.8 * WINDOW_HOURS:
                continue
            r = float(np.corrcoef(t[paired], rh[paired])[0, 1])
            days_t, days_rh = t.reshape(-1, 24), rh.reshape(-1, 24)
            amplitude = float(np.nanmedian(np.nanmax(days_rh, axis=1) - np.nanmin(days_rh, axis=1)))
            gate_ok = r < -0.5 and amplitude > 15
            gate_log[f"{code}_{year}"] = {"pearson_t_rh": round(r, 3), "median_daily_rh_amplitude": round(amplitude, 1), "passes": gate_ok}
            hour_of_tmax = [int(np.nanargmax(d)) for d in days_t if np.isfinite(d).sum() >= 18]
            time_check[f"{code}_{year}"] = float(np.median(hour_of_tmax)) if hour_of_tmax else None
            if not gate_ok:
                continue
            values = vpd(t, rh).reshape(-1, 24)
            valid_days = np.isfinite(values).sum(axis=1) >= 18
            daily_max = np.where(valid_days, np.nanmax(np.where(np.isfinite(values), values, -np.inf), axis=1), np.nan)
            series_by_year[year] = daily_max
        if TARGET not in series_by_year:
            continue
        reference_years = [y for y in series_by_year if y != TARGET]
        if len(reference_years) < 5:
            continue
        station_metric = {y: float(np.nanmean(series_by_year[y])) for y in series_by_year}

        era5_metric, era5_daily_2026 = {}, None
        for y in series_by_year:
            cube = grid_vpd[grid_seasons.index(y)][:, nearest].astype(float)
            local = {datetime(y, 8, 1) + timedelta(hours=h) - timedelta(hours=5): v for h, v in enumerate(cube)}
            maxima = []
            for d in range(LAST_DAY - FIRST_DAY + 1):
                if not np.isfinite(series_by_year[y][d]):
                    maxima.append(np.nan)
                    continue
                day = datetime(y, 8, FIRST_DAY + d)
                vals = [local[day + timedelta(hours=h)] for h in range(24) if day + timedelta(hours=h) in local]
                maxima.append(max(vals) if vals else np.nan)
            maxima = np.asarray(maxima)
            era5_metric[y] = float(np.nanmean(maxima))
            if y == TARGET:
                era5_daily_2026 = maxima
        both = np.isfinite(series_by_year[TARGET]) & np.isfinite(era5_daily_2026)
        station_rows.append({
            "station_code": code, "station_name": meta["name"], "municipality": meta["municipality"],
            "latitude": meta["latitude"], "longitude": meta["longitude"], "reference_years": len(reference_years),
            "station_vpd_daily_max_mean_2026_kpa": round(station_metric[TARGET], 4),
            "station_reference_median_kpa": round(float(np.median([station_metric[y] for y in reference_years])), 4),
            "station_percentile_2026": percentile(station_metric[TARGET], [station_metric[y] for y in reference_years]),
            "era5_nearest_percentile_2026": percentile(era5_metric[TARGET], [era5_metric[y] for y in reference_years]),
            "pearson_daily_station_era5_2026": round(float(np.corrcoef(series_by_year[TARGET][both], era5_daily_2026[both])[0, 1]), 3) if both.sum() > 3 else "",
            "era5_minus_station_bias_2026_kpa": round(era5_metric[TARGET] - station_metric[TARGET], 4),
        })

    gate_2026 = [v["passes"] for k, v in gate_log.items() if k.endswith(f"_{TARGET}")]
    hours_tmax = [v for v in time_check.values() if v is not None]
    median_hour = float(np.median(hours_tmax)) if hours_tmax else None
    if median_hour is None or not (11 <= median_hour <= 16):
        raise RuntimeError(f"La hora mediana del máximo de temperatura es {median_hour}, no compatible con hora local")
    evaluable = bool(gate_2026) and sum(gate_2026) > len(gate_2026) / 2
    percentiles = [r["station_percentile_2026"] for r in station_rows]
    network = None
    if evaluable and percentiles:
        median_pct = float(np.median(percentiles))
        network = {
            "stations": len(percentiles),
            "median_station_percentile_2026": median_pct,
            "lower_tercile": sum(p <= 33.3 for p in percentiles),
            "middle_tercile": sum(33.3 < p < 66.7 for p in percentiles),
            "upper_tercile": sum(p >= 66.7 for p in percentiles),
            "median_era5_nearest_percentile_2026": float(np.median([r["era5_nearest_percentile_2026"] for r in station_rows])),
            "verdict": "confirms_low_demand" if median_pct <= 33.3 else ("contradicts" if median_pct >= 66.7 else "intermediate"),
        }
    if station_rows:
        STATION_OUT.parent.mkdir(parents=True, exist_ok=True)
        with STATION_OUT.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(station_rows[0]))
            writer.writeheader()
            writer.writerows(station_rows)
    audit = {
        "script": Path(__file__).name,
        "retrieved": date.today().isoformat(),
        "plan_sha256": sha256(PLAN), "amendment_sha256": sha256(AMENDMENT),
        "status": "descriptivo y posterior",
        "stations_with_both_variables_2026": len(stations),
        "sensor_descriptions": {k: sorted([list(x) for x in v]) for k, v in descriptions.items()},
        "out_of_range_discarded": dict(out_of_range),
        "median_hour_of_daily_temperature_maximum": median_hour,
        "humidity_gate_2026_passes": f"{sum(gate_2026)} of {len(gate_2026)}",
        "humidity_gate": gate_log,
        "part_b_evaluable": evaluable,
        "network": network,
        "stations": station_rows,
        "raw_files": raw_files,
        "limits": json.loads(PLAN.read_text(encoding="utf-8"))["part_b_ideam_stations"]["limits"],
    }
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=lambda o: o.item()) + "\n", encoding="utf-8")
    print(json.dumps({k: audit[k] for k in ("stations_with_both_variables_2026", "sensor_descriptions", "median_hour_of_daily_temperature_maximum",
                                            "humidity_gate_2026_passes", "part_b_evaluable", "network")}, ensure_ascii=False, indent=2,
                     default=lambda o: o.item()))
    for r in station_rows:
        print(r["station_name"], r["reference_years"], r["station_percentile_2026"], r["era5_nearest_percentile_2026"], r["pearson_daily_station_era5_2026"])


if __name__ == "__main__":
    main()
