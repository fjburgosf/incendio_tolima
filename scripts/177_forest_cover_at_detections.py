"""Cobertura previa al fuego y áreas protegidas bajo las detecciones VIIRS de Tolima.

Sigue `configs/forest_cover_detection_plan_v1.json`, congelado antes de descargar
los mapas y de muestrear. Para cada detección de agosto en Tolima de 2012 a 2026
lee la clase MapBiomas colección 3 del año anterior en el píxel de 30 metros y en
una ventana de 13 por 13 píxeles. Lee solo las teselas del GeoTIFF que contienen
detecciones. Después cruza las detecciones con el recorte RUNAP de Tolima y con
los municipios de CORTOLIMA. Análisis descriptivo y posterior.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import openpyxl
import tifffile
from shapely import STRtree, points
from shapely.geometry import shape


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "configs" / "forest_cover_detection_plan_v1.json"
DETECTIONS = ROOT / "data" / "interim" / "regional_fire_detections_labelled_v1.csv"
RASTERS = ROOT / "data" / "raw" / "mapbiomas" / "coverage_collection3"
STATISTICS = ROOT / "data" / "raw" / "mapbiomas" / "mapbiomas_colombia_collection3_coverage_statistics.zip"
RUNAP = ROOT / "data" / "interim" / "runap_protected_areas_tolima_exact.geojson"
MUNICIPALITIES = ROOT / "data" / "raw" / "boundaries" / "cortolima_limite_municipal_25k.geojson"
ROWS_OUT = ROOT / "data" / "processed" / "tolima_detection_cover_v1.csv"
SEASON_OUT = ROOT / "results" / "exploratory" / "tolima_detection_cover_by_season_v1.csv"
AUDIT = ROOT / "results" / "exploratory" / "audit_forest_cover_detections_v1.json"
TARGET = 2026
REFERENCE = list(range(2012, 2026))
HALF_WINDOW = 6
STAT_GROUPS = {
    "1.1. Bosque": "forest_formation", "1.2. Manglar": "forest_formation", "1.3. Bosque inundable": "forest_formation",
    "3.1. Silvicultura": "forest_plantation",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def lzw_decode(data: bytes, size: int) -> bytes:
    """Decodificador LZW de TIFF en Python puro, con cambio temprano de ancho de código.

    Se usa porque la política de control de aplicaciones del equipo bloquea la
    biblioteca compilada de imagecodecs. El resultado se contrasta en ubicaciones
    conocidas antes del análisis.
    """
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    out = bytearray()
    bits = 0
    nbits = 0
    width = 9
    previous = None
    position = 0
    n = len(data)
    while position < n or nbits >= width:
        while nbits < width and position < n:
            bits = (bits << 8) | data[position]
            position += 1
            nbits += 8
        if nbits < width:
            break
        nbits -= width
        code = (bits >> nbits) & ((1 << width) - 1)
        if code == 257:
            break
        if code == 256:
            table = table[:258]
            width = 9
            previous = None
            continue
        if previous is None:
            entry = table[code]
            out += entry
            previous = entry
            continue
        if code < len(table):
            entry = table[code]
            table.append(previous + entry[:1])
        else:
            entry = previous + previous[:1]
            table.append(entry)
        out += entry
        previous = entry
        if len(table) + 1 >= (1 << width) and width < 12:
            width += 1
        if len(out) >= size:
            break
    return bytes(out[:size])


class TiledRaster:
    """Lector de un GeoTIFF geográfico que decodifica solo las teselas pedidas."""

    def __init__(self, path: Path) -> None:
        self.handle = tifffile.TiffFile(path)
        self.page = self.handle.pages[0]
        tags = self.page.tags
        scale = tags["ModelPixelScaleTag"].value
        tie = tags["ModelTiepointTag"].value
        geokeys = tags["GeoKeyDirectoryTag"].value
        keys = {geokeys[i]: geokeys[i + 3] for i in range(4, len(geokeys), 4)}
        if keys.get(1024) != 2 or keys.get(2048) not in (4326,):
            raise RuntimeError(f"El ráster no está en coordenadas geográficas WGS84, claves {keys}")
        self.dx, self.dy = float(scale[0]), float(scale[1])
        self.x0 = float(tie[3]) - float(tie[0]) * self.dx
        self.y0 = float(tie[4]) + float(tie[1]) * self.dy
        self.height, self.width = self.page.shape[:2]
        if not self.page.is_tiled:
            raise RuntimeError("Se esperaba un GeoTIFF por teselas")
        self.tile_h, self.tile_w = self.page.tilelength, self.page.tilewidth
        self.tiles_across = -(-self.width // self.tile_w)
        self.cache: dict[int, np.ndarray] = {}
        self.geokeys = keys

    def tile(self, ty: int, tx: int) -> np.ndarray:
        index = ty * self.tiles_across + tx
        if index not in self.cache:
            fh = self.handle.filehandle
            fh.seek(self.page.dataoffsets[index])
            data = fh.read(self.page.databytecounts[index])
            if self.page.compression != 5 or self.page.predictor != 1 or self.page.dtype != np.uint8:
                raise RuntimeError("Se esperaba LZW sin predictor y enteros de 8 bits")
            raw = lzw_decode(data, self.tile_h * self.tile_w)
            if len(raw) != self.tile_h * self.tile_w:
                raise RuntimeError(f"Tesela {index} incompleta tras descomprimir")
            self.cache[index] = np.frombuffer(raw, dtype=np.uint8).reshape(self.tile_h, self.tile_w)
        return self.cache[index]

    def window(self, lon: float, lat: float, half: int) -> np.ndarray:
        col = int(np.floor((lon - self.x0) / self.dx))
        row = int(np.floor((self.y0 - lat) / self.dy))
        out = np.zeros((2 * half + 1, 2 * half + 1), dtype=np.int32)
        for i, r in enumerate(range(row - half, row + half + 1)):
            for j, c in enumerate(range(col - half, col + half + 1)):
                if 0 <= r < self.height and 0 <= c < self.width:
                    out[i, j] = self.tile(r // self.tile_h, c // self.tile_w)[r % self.tile_h, c % self.tile_w]
        return out


def position(observed: float, reference: list[float]) -> dict:
    values = np.asarray(reference, dtype=float)
    below, equal = int(np.sum(values < observed)), int(np.sum(values == observed))
    return {"observed": round(observed, 6), "reference_min": round(float(values.min()), 6), "reference_median": round(float(np.median(values)), 6),
            "reference_max": round(float(values.max()), 6), "empirical_percentile": round(100.0 * (below + 0.5 * equal) / values.size, 2)}


def tolima_area_shares() -> dict[int, dict[str, float]]:
    with zipfile.ZipFile(STATISTICS) as archive:
        workbook = openpyxl.load_workbook(io.BytesIO(archive.read("Statistics-for-Website-MB-Cobertura-col3.xlsx")), read_only=True)
    rows = workbook["COBERTURA_DEPARTAMENTO"].iter_rows(values_only=True)
    head = list(next(rows))
    totals: dict[int, Counter] = defaultdict(Counter)
    for row in rows:
        if row[0] != "Tolima":
            continue
        level1, level2 = str(row[3]), str(row[4])
        if level2 in STAT_GROUPS:
            group = STAT_GROUPS[level2]
        elif level1.startswith("1."):
            group = "forest_formation"
        elif level1.startswith("2."):
            group = "natural_non_forest"
        elif level1.startswith("3."):
            group = "agricultural_other"
        elif level1.startswith("4."):
            group = "non_vegetated"
        elif level1.startswith("5."):
            group = "water"
        else:
            group = "not_observed"
        for year in range(2011, 2025):
            value = row[head.index(year)]
            if value:
                totals[year][group] += float(value)
    return {year: {g: v / sum(c.values()) for g, v in c.items()} for year, c in totals.items()}


def main() -> None:
    if AUDIT.exists() or ROWS_OUT.exists():
        raise FileExistsError("Las salidas ya existen y no se sobrescriben")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    code_to_group = {code: group for group, codes in plan["groups"].items() if isinstance(codes, list) for code in codes}
    groups = [g for g, codes in plan["groups"].items() if isinstance(codes, list)]

    detections = []
    with DETECTIONS.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["department"] == "Tolima" and 2012 <= int(row["season"]) <= TARGET:
                detections.append(row)
    by_cover_year: dict[int, list[int]] = defaultdict(list)
    for position_index, row in enumerate(detections):
        by_cover_year[min(int(row["season"]) - 1, 2024)].append(position_index)

    records = [None] * len(detections)
    raster_info = {}
    for cover_year, indices in sorted(by_cover_year.items()):
        path = RASTERS / f"colombia_coverage_{cover_year}.tif"
        raster = TiledRaster(path)
        raster_info[cover_year] = {"path": str(path.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(path),
                                   "pixel_degrees": [raster.dx, raster.dy], "geokeys": {str(k): v for k, v in raster.geokeys.items()}}
        for i in indices:
            row = detections[i]
            values = raster.window(float(row["longitude"]), float(row["latitude"]), HALF_WINDOW)
            unknown = set(np.unique(values).tolist()) - set(code_to_group)
            if unknown:
                raise RuntimeError(f"Código de cobertura fuera de la leyenda congelada {sorted(unknown)} en {cover_year}")
            centre = int(values[HALF_WINDOW, HALF_WINDOW])
            counts = Counter(code_to_group[int(v)] for v in values.ravel())
            total = values.size
            records[i] = {
                "season": int(row["season"]), "acq_date": row["acq_date"], "latitude": float(row["latitude"]), "longitude": float(row["longitude"]),
                "cover_year": cover_year, "cover_code": centre, "cover_group": code_to_group[centre],
                **{f"window_fraction_{g}": round(counts[g] / total, 6) for g in groups},
            }
        raster.handle.close()

    runap = json.loads(RUNAP.read_text(encoding="utf-8"))["features"]
    runap_geoms = [shape(f["geometry"]) for f in runap]
    municipalities = json.loads(MUNICIPALITIES.read_text(encoding="utf-8"))["features"]
    municipal_geoms = [shape(f["geometry"]) for f in municipalities]
    pts = points([[r["longitude"], r["latitude"]] for r in records])
    runap_tree, municipal_tree = STRtree(runap_geoms), STRtree(municipal_geoms)
    runap_hits = runap_tree.query(pts, predicate="intersects")
    municipal_hits = municipal_tree.query(pts, predicate="intersects")
    categories: dict[int, set] = defaultdict(set)
    for p, g in zip(*runap_hits):
        categories[int(p)].add(runap[int(g)]["properties"]["category"])
    municipality: dict[int, list] = defaultdict(list)
    for p, g in zip(*municipal_hits):
        municipality[int(p)].append(municipalities[int(g)]["properties"]["mun_nombre"])
    for i, record in enumerate(records):
        record["in_runap"] = bool(categories.get(i))
        record["runap_categories"] = "|".join(sorted(categories.get(i, [])))
        record["municipality"] = municipality[i][0] if len(municipality[i]) == 1 else ("" if not municipality[i] else "AMBIGUOUS")

    ROWS_OUT.parent.mkdir(parents=True, exist_ok=True)
    with ROWS_OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)

    shares = tolima_area_shares()
    season_rows = []
    for season in REFERENCE + [TARGET]:
        subset = [r for r in records if r["season"] == season]
        n = len(subset)
        cover_year = min(season - 1, 2024)
        row = {"season": season, "detections": n, "cover_year": cover_year}
        for g in groups:
            share = sum(r["cover_group"] == g for r in subset) / n
            row[f"share_point_{g}"] = round(share, 6)
            row[f"share_window_mean_{g}"] = round(float(np.mean([r[f"window_fraction_{g}"] for r in subset])), 6)
            area = shares[cover_year].get(g, 0.0)
            row[f"tolima_area_share_{g}"] = round(area, 6)
            row[f"selection_ratio_point_{g}"] = round(share / area, 4) if area > 0 else ""
        row["share_window_majority_forest"] = round(sum(r["window_fraction_forest_formation"] >= 0.5 for r in subset) / n, 6)
        row["share_in_runap"] = round(sum(r["in_runap"] for r in subset) / n, 6)
        row["detections_point_forest"] = sum(r["cover_group"] == "forest_formation" for r in subset)
        row["detections_in_runap"] = sum(r["in_runap"] for r in subset)
        season_rows.append(row)
    SEASON_OUT.parent.mkdir(parents=True, exist_ok=True)
    with SEASON_OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(season_rows[0]))
        writer.writeheader()
        writer.writerows(season_rows)

    by_season = {r["season"]: r for r in season_rows}
    metrics = ["share_point_forest_formation", "share_window_mean_forest_formation", "share_window_majority_forest",
               "share_point_natural_non_forest", "share_point_agricultural_other", "share_point_forest_plantation", "share_in_runap"]
    positioning = {m: position(by_season[TARGET][m], [by_season[s][m] for s in REFERENCE]) for m in metrics}
    target = [r for r in records if r["season"] == TARGET]
    forest_2026_by_municipality = Counter(r["municipality"] for r in target if r["cover_group"] == "forest_formation")
    all_2026_by_municipality = Counter(r["municipality"] for r in target)
    runap_2026 = Counter(c for r in target for c in r["runap_categories"].split("|") if c)
    runap_2026_names = Counter()
    for p, g in zip(*runap_hits):
        if records[int(p)]["season"] == TARGET:
            runap_2026_names[runap[int(g)]["properties"]["name"]] += 1
    reference_forest_counts = Counter(r["municipality"] for r in records if r["season"] in REFERENCE and r["cover_group"] == "forest_formation")

    audit = {
        "script": Path(__file__).name,
        "plan_sha256": sha256(PLAN),
        "status": "descriptivo y posterior",
        "detections": len(records),
        "rasters": raster_info,
        "positioning_2026": positioning,
        "season_2026": by_season[TARGET],
        "forest_point_detections_2026_by_municipality": dict(forest_2026_by_municipality.most_common()),
        "all_detections_2026_by_municipality": dict(all_2026_by_municipality.most_common()),
        "forest_point_detections_2012_2025_by_municipality": dict(reference_forest_counts.most_common(15)),
        "runap_2026_detections_by_category": dict(runap_2026),
        "runap_2026_detections_by_area": dict(runap_2026_names.most_common()),
        "municipality_unassigned": sum(1 for r in records if r["municipality"] in ("", "AMBIGUOUS")),
        "inputs": [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha256(p)} for p in (DETECTIONS, STATISTICS, RUNAP, MUNICIPALITIES)],
        "outputs": [str(ROWS_OUT.relative_to(ROOT)).replace("\\", "/"), str(SEASON_OUT.relative_to(ROOT)).replace("\\", "/")],
        "limits": plan["interpretation_limits"],
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: audit[k] for k in ("detections", "positioning_2026", "forest_point_detections_2026_by_municipality",
                                            "runap_2026_detections_by_category", "runap_2026_detections_by_area", "municipality_unassigned")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
