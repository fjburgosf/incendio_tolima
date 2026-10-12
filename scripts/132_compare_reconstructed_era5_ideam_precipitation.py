"""Describe agreement between audited ERA5 Land intervals and complete IDEAM gauges."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.stats import pearsonr, spearmanr


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "precipitation_reconstruction_and_exploratory_analysis_plan_v1.json"
RAW = ROOT / "data" / "raw" / "ideam"
LINK = ROOT / "data" / "interim" / "ideam_precipitation_station_era5_linkage.csv"
COVERAGE = ROOT / "data" / "interim" / "ideam_precipitation_station_recent_coverage.csv"
GRID = ROOT / "data" / "interim" / "era5_land_hourly_precipitation_interval_v2.npz"
DAILY_OUT = ROOT / "results" / "exploratory" / "era5_ideam_daily_precipitation_comparison_v2.csv"
MONTHLY_OUT = ROOT / "results" / "exploratory" / "era5_ideam_monthly_precipitation_comparison_v2.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_era5_ideam_precipitation_comparison_v2.json"
LOCAL = timezone(timedelta(hours=-5))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def statistics(rows: list[dict[str, object]]) -> dict[str, object]:
    observed = np.asarray([float(row["ideam_gauge_precipitation_mm"]) for row in rows], dtype=float)
    estimated = np.asarray([float(row["era5_nearest_cell_precipitation_mm"]) for row in rows], dtype=float)
    difference = estimated - observed
    return {
        "comparison_count": int(observed.size),
        "mean_signed_difference_mm": float(np.mean(difference)),
        "median_signed_difference_mm": float(np.median(difference)),
        "mean_absolute_error_mm": float(np.mean(np.abs(difference))),
        "root_mean_squared_error_mm": float(np.sqrt(np.mean(np.square(difference)))),
        "pearson_correlation": float(pearsonr(observed, estimated).statistic),
        "spearman_correlation": float(spearmanr(observed, estimated).statistic),
    }


def main() -> None:
    for path in (DAILY_OUT, MONTHLY_OUT, AUDIT):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {path}")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    with COVERAGE.open(encoding="utf-8-sig", newline="") as handle:
        complete = {row["station_code"] for row in csv.DictReader(handle) if row["june_to_august_2026_coverage_percent"] == "100.0"}
    with LINK.open(encoding="utf-8-sig", newline="") as handle:
        stations = [row for row in csv.DictReader(handle) if row["station_code"] in complete]
    if len(stations) != 3:
        raise RuntimeError(f"Expected exactly three complete audited stations, found {len(stations)}")
    gauge_values = {row["station_code"]: {} for row in stations}
    for source in sorted(RAW.glob("Precipitacion*.csv")):
        with source.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                station_code = row["CodigoEstacion"].strip()
                date = row["Fecha"][:10]
                if station_code in gauge_values and "2026-06-01" <= date <= "2026-08-31":
                    if date in gauge_values[station_code]:
                        raise RuntimeError(f"Duplicate daily gauge record {station_code} {date}")
                    gauge_values[station_code][date] = float(row["Valor"].replace(",", "."))
    dates = [(datetime(2026, 6, 1) + timedelta(days=index)).date().isoformat() for index in range(92)]
    for station_code, series in gauge_values.items():
        if set(series) != set(dates):
            raise RuntimeError(f"Incomplete daily gauge series for {station_code}")
    archive = np.load(GRID, allow_pickle=False)
    times = [parse_time(str(value)) for value in archive["validity_utc"]]
    precipitation_key = "tp_interval_m" if "tp_interval_m" in archive.files else "tp_increment_m"
    precipitation = np.asarray(archive[precipitation_key], dtype=float) * 1000.0
    latitudes = np.asarray(archive["latitudes"], dtype=float)
    longitudes = np.asarray(archive["longitudes"], dtype=float)
    if precipitation.shape != (len(times), len(latitudes)):
        raise RuntimeError("Unexpected reconstructed precipitation shape")
    daily_rows: list[dict[str, object]] = []
    for station in stations:
        distance_sq = (latitudes - float(station["nearest_global_grid_latitude"])) ** 2 + (longitudes - float(station["nearest_global_grid_longitude"])) ** 2
        index = int(np.argmin(distance_sq))
        if not np.isclose(latitudes[index], float(station["nearest_global_grid_latitude"])) or not np.isclose(longitudes[index], float(station["nearest_global_grid_longitude"])):
            raise RuntimeError(f"Nearest-cell linkage mismatch for station {station['station_code']}")
        for date in dates:
            local_start = datetime.fromisoformat(date).replace(tzinfo=LOCAL)
            local_end = local_start + timedelta(days=1)
            start_utc = local_start.astimezone(timezone.utc)
            end_utc = local_end.astimezone(timezone.utc)
            rows = [position for position, time in enumerate(times) if start_utc < time <= end_utc]
            if len(rows) != 24:
                raise RuntimeError(f"Incomplete ERA5 local daily window for {station['station_code']} {date}")
            gauge = gauge_values[station["station_code"]][date]
            era5 = float(np.sum(precipitation[rows, index]))
            daily_rows.append(
                {
                    "station_code": station["station_code"],
                    "station_name": station["station_name"],
                    "municipality": station["official_municipality"],
                    "date_local": date,
                    "ideam_gauge_precipitation_mm": gauge,
                    "era5_nearest_cell_precipitation_mm": era5,
                    "difference_era5_minus_ideam_mm": era5 - gauge,
                }
            )
    monthly_rows: list[dict[str, object]] = []
    for station in stations:
        station_rows = [row for row in daily_rows if row["station_code"] == station["station_code"]]
        for month in (6, 7, 8):
            month_rows = [row for row in station_rows if int(str(row["date_local"])[5:7]) == month]
            gauge = float(sum(float(row["ideam_gauge_precipitation_mm"]) for row in month_rows))
            era5 = float(sum(float(row["era5_nearest_cell_precipitation_mm"]) for row in month_rows))
            monthly_rows.append(
                {
                    "station_code": station["station_code"],
                    "station_name": station["station_name"],
                    "municipality": station["official_municipality"],
                    "month_local": f"2026-{month:02d}",
                    "ideam_gauge_precipitation_mm": gauge,
                    "era5_nearest_cell_precipitation_mm": era5,
                    "difference_era5_minus_ideam_mm": era5 - gauge,
                }
            )
    write_csv(DAILY_OUT, daily_rows)
    write_csv(MONTHLY_OUT, monthly_rows)
    audit = {
        "plan": {"file": str(PLAN.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(PLAN)},
        "station_codes": [station["station_code"] for station in stations],
        "daily": statistics(daily_rows),
        "monthly": statistics(monthly_rows),
        "inputs": {
            "station_linkage": {"file": str(LINK.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(LINK)},
            "station_coverage": {"file": str(COVERAGE.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(COVERAGE)},
            "era5_precipitation_intervals": {"file": str(GRID.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(GRID)},
        },
        "outputs": {
            "daily": {"file": str(DAILY_OUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(DAILY_OUT)},
            "monthly": {"file": str(MONTHLY_OUT.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(MONTHLY_OUT)},
        },
        "interpretation_limit": plan["gauge_comparison"]["interpretation_limit"],
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"daily": audit["daily"], "monthly": audit["monthly"], "audit": str(AUDIT.relative_to(ROOT))}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
