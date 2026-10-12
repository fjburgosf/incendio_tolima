"""Download and audit the current IDEAM heat-monitor snapshot for Tolima."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "ideam_heat_monitor"
OUTPUT = ROOT / "results" / "exploratory" / "audit_ideam_heat_monitor_tolima_snapshot_2026_09_06.json"
BASE = (
    "https://visualizador.ideam.gov.co/gisserver/rest/services/"
    "Tematica/Monitoreo_Inc_Cobertura_AP/FeatureServer"
)
ACCESS_DATE = "2026-09-06"
LAYERS = {
    2: ("current_points", "cod_dane='73'"),
    3: ("current_enriched_footprints", "cod_dane_dpto='73'"),
    6: ("seven_day_points", "cod_dane='73'"),
    7: ("seven_day_enriched_footprints", "cod_dane_dpto='73'"),
}


def preserve_raw(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError(f"Raw file changed and will not be overwritten: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest().upper()


def epoch_to_date(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value[:10]
    return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).date().isoformat()


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("El resultado ya existe y no se sobrescribirá")

    audits = []
    for layer_id, (label, where) in LAYERS.items():
        response = requests.get(
            f"{BASE}/{layer_id}/query",
            params={
                "where": where,
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": "4326",
                "f": "json",
            },
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(json.dumps(payload["error"], ensure_ascii=False))
        path = RAW_DIR / f"{label}_tolima_snapshot_{ACCESS_DATE}.json"
        preserve_raw(path, response.content)

        features = payload.get("features", [])
        attributes = [feature.get("attributes", {}) for feature in features]
        dates = [epoch_to_date(item.get("acq_date")) for item in attributes]
        dates = [item for item in dates if item is not None]
        point_ids = [item.get("idptocalor") for item in attributes if item.get("idptocalor") is not None]
        geometry_types = Counter(
            "rings" if "rings" in feature.get("geometry", {}) else "point"
            for feature in features
        )
        coverage_values = Counter(
            str(item.get("leyenda")) for item in attributes if item.get("leyenda") is not None
        )
        fuel_values = Counter(
            str(item.get("tipocombst")) for item in attributes if item.get("tipocombst") is not None
        )
        area_values = [item.get("area_ha") for item in attributes if item.get("area_ha") is not None]

        audits.append(
            {
                "layer_id": layer_id,
                "label": label,
                "where": where,
                "raw_path": str(path.relative_to(ROOT)).replace("\\", "/"),
                "raw_sha256": sha256_bytes(response.content),
                "feature_count": len(features),
                "exceeded_transfer_limit": payload.get("exceededTransferLimit", False),
                "geometry_encodings": dict(sorted(geometry_types.items())),
                "unique_heat_point_ids": len(set(point_ids)),
                "rows_per_heat_point_id_max": max(Counter(point_ids).values()) if point_ids else None,
                "acquisition_date_min": min(dates) if dates else None,
                "acquisition_date_max": max(dates) if dates else None,
                "acquisition_date_counts": dict(sorted(Counter(dates).items())),
                "source_counts": dict(
                    sorted(Counter(str(item.get("fuente")) for item in attributes).items())
                ),
                "municipality_counts": dict(
                    sorted(Counter(str(item.get("nom_mpio")) for item in attributes).items())
                ),
                "coverage_class_counts": dict(sorted(coverage_values.items())),
                "fuel_type_counts": dict(sorted(fuel_values.items())),
                "area_ha_present_count": len(area_values),
                "area_ha_distinct_count": len(set(area_values)),
                "area_ha_interpretation": (
                    "Field is present in enriched layers but is not interpreted as heat-pixel area "
                    "or burned area. Repeated values across fragments require source documentation."
                    if area_values
                    else None
                ),
            }
        )

    result = {
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "official_service": BASE,
        "layers": audits,
        "interpretation_limit": (
            "This is a mutable current or seven-day operational snapshot. It is not a complete "
            "August archive. Enriched polygon rows can repeat one heat-point identifier because "
            "a footprint intersects multiple mapped classes. They are not independent fires, "
            "burned-area perimeters, or validated severity."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
