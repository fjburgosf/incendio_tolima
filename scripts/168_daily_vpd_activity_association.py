"""Evalúa la asociación diaria entre demanda atmosférica y actividad térmica.

Motivo. El posicionamiento estacional compara agregados mensuales de agosto. Un
promedio mensual comprime la estructura diaria a la que responde el fuego, de modo
que un valor mensual bajo no descarta ventanas diarias de alta demanda. Este
análisis comprueba esa objeción de forma directa.

Estatus. Exploratorio y declarado como tal. No estaba en el plan congelado del
módulo estacional. Se añade después de observar los resultados estacionales, por
lo cual sus valores p se reportan sin corrección por multiplicidad y con la
advertencia de que los días consecutivos están autocorrelacionados.

La unidad es el día de agosto en Tolima. La exposición es el máximo diario del
déficit de presión de vapor promediado sobre los 198 centros retenidos. El
desenlace es el número de detecciones VIIRS Suomi NPP asignadas a Tolima ese día.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import stats


ROOT = Path(__file__).resolve().parents[1]
DANGER = ROOT / "data" / "processed" / "era5_land_august_daily_danger_1991_2026_v1.csv"
DETECTIONS = ROOT / "data" / "interim" / "regional_fire_detections_labelled_v1.csv"
OUTPUT = ROOT / "data" / "processed" / "tolima_daily_vpd_activity_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_daily_vpd_activity_v1.json"
SEED = 20260921
PERMUTATIONS = 20000


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def read_csv(path: Path) -> list:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    danger = {row["date_utc"]: row for row in read_csv(DANGER)}
    counts = defaultdict(int)
    seasons = set()
    for row in read_csv(DETECTIONS):
        if row["department"] != "Tolima":
            continue
        counts[row["acq_date"]] += 1
        seasons.add(int(row["season"]))

    records = []
    for date, row in sorted(danger.items()):
        season = int(row["season"])
        if season not in seasons:
            continue
        records.append(
            {
                "season": season,
                "date": date,
                "vpd_daily_max_kpa": float(row["vapour_pressure_deficit_max_kpa"]),
                "vpd_afternoon_kpa": float(row["vapour_pressure_deficit_afternoon_kpa"]),
                "detections": counts.get(date, 0),
            }
        )

    if len(records) != 15 * 31:
        raise RuntimeError(f"Se esperaban 465 dias y se obtuvieron {len(records)}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)

    generator = np.random.default_rng(SEED)
    results = {}

    for scope, subset in (
        ("all_seasons_2012_2026", records),
        ("reference_seasons_2012_2025", [r for r in records if r["season"] != 2026]),
        ("target_season_2026", [r for r in records if r["season"] == 2026]),
    ):
        exposure = np.asarray([r["vpd_daily_max_kpa"] for r in subset])
        outcome = np.asarray([r["detections"] for r in subset], dtype=float)
        rho, p_value = stats.spearmanr(exposure, outcome)
        active = outcome > 0
        difference = float(exposure[active].mean() - exposure[~active].mean()) if active.any() and (~active).any() else float("nan")
        if np.isfinite(difference):
            permuted = np.empty(PERMUTATIONS)
            for index in range(PERMUTATIONS):
                shuffled = generator.permutation(exposure)
                permuted[index] = shuffled[: active.sum()].mean() - shuffled[active.sum():].mean()
            permutation_p = float((np.abs(permuted) >= abs(difference)).mean())
            # Circular shift within each season preserves the temporal ordering of the
            # exposure, so consecutive day autocorrelation is not destroyed by the test.
            season_index = np.asarray([r["season"] for r in subset])
            shifted_statistics = []
            for _ in range(PERMUTATIONS):
                rotated = np.empty_like(exposure)
                for season_value in np.unique(season_index):
                    mask = season_index == season_value
                    block = exposure[mask]
                    offset = int(generator.integers(0, block.size))
                    rotated[mask] = np.roll(block, offset)
                shifted_statistics.append(rotated[active].mean() - rotated[~active].mean())
            shifted = np.asarray(shifted_statistics)
            circular_p = float((np.abs(shifted) >= abs(difference)).mean())
        else:
            permutation_p = float("nan")
            circular_p = float("nan")
        results[scope] = {
            "days": len(subset),
            "days_with_detection": int(active.sum()),
            "spearman_rho": round(float(rho), 4),
            "spearman_p": round(float(p_value), 6),
            "mean_vpd_days_with_detection_kpa": round(float(exposure[active].mean()), 4) if active.any() else None,
            "mean_vpd_days_without_detection_kpa": round(float(exposure[~active].mean()), 4) if (~active).any() else None,
            "difference_kpa": round(difference, 4),
            "permutation_p_two_sided": permutation_p,
            "circular_shift_p_two_sided": circular_p,
        }

    reference = [r for r in records if r["season"] != 2026]
    threshold = float(np.percentile([r["vpd_daily_max_kpa"] for r in reference], 90))
    by_season = defaultdict(lambda: {"days_above": 0, "detections_above": 0, "detections_total": 0})
    for record in records:
        bucket = by_season[record["season"]]
        bucket["detections_total"] += record["detections"]
        if record["vpd_daily_max_kpa"] > threshold:
            bucket["days_above"] += 1
            bucket["detections_above"] += record["detections"]

    season_table = []
    for season in sorted(by_season):
        bucket = by_season[season]
        share = bucket["detections_above"] / bucket["detections_total"] if bucket["detections_total"] else 0.0
        season_table.append(
            {
                "season": season,
                "days_above_reference_p90": bucket["days_above"],
                "detections_on_those_days": bucket["detections_above"],
                "share_of_season_detections": round(share, 4),
            }
        )

    audit = {
        "script": Path(__file__).name,
        "executed": "2026-09-21",
        "status": "exploratorio, no prefijado, sin correccion por multiplicidad",
        "unit": "dia de agosto en Tolima",
        "seed": SEED,
        "permutations": PERMUTATIONS,
        "inputs": [
            {"path": str(DANGER.relative_to(ROOT)), "sha256": sha256(DANGER)},
            {"path": str(DETECTIONS.relative_to(ROOT)), "sha256": sha256(DETECTIONS)},
        ],
        "association_by_scope": results,
        "reference_p90_threshold_kpa": round(threshold, 4),
        "days_above_threshold_by_season": season_table,
        "interpretation_limits": [
            "La permutacion independiente destruye la autocorrelacion de dias consecutivos y su valor p es optimista. Por eso el contraste primario es la permutacion por desplazamiento circular dentro de cada temporada, que preserva el orden temporal.",
            "Una deteccion no es un incendio y el conteo diario depende del paso orbital y de la nubosidad.",
            "La asociacion es descriptiva y no identifica un mecanismo.",
        ],
        "outputs": [str(OUTPUT.relative_to(ROOT))],
    }
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"umbral p90 de referencia {round(threshold, 4)} kPa")
    for row in season_table:
        if row["season"] in (2019, 2026):
            print(row)


if __name__ == "__main__":
    main()
