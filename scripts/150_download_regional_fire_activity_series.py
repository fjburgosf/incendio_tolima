"""Descarga la serie regional de detecciones VIIRS Suomi NPP para agosto.

Los criterios quedaron congelados en
`configs/regional_fire_activity_series_plan_v1.json` antes de ejecutar y antes de
observar ningún conteo.

Descarga procesamiento estándar para las temporadas de referencia y tiempo casi
real solo para 2026, conservando el nivel de procesamiento en el nombre del
archivo y en el manifiesto. Los archivos crudos no se sobrescriben. La clave se
lee del entorno y nunca se escribe en salidas.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
PLAN_PATH = ROOT / "configs" / "regional_fire_activity_series_plan_v1.json"
RAW_DIR = ROOT / "data" / "raw" / "firms_regional_august_series_2026-09-09"
MANIFEST = RAW_DIR / "manifest.json"
AUDIT = ROOT / "results" / "exploratory" / "audit_regional_fire_activity_download_2026-09-09.json"
TIMEOUT = 300
PAUSE_SECONDS = 2


def read_map_key() -> str:
    key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    if key:
        return key
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "FIRMS_MAP_KEY":
                return value.strip()
    raise RuntimeError("No se encontró FIRMS_MAP_KEY en el entorno ni en .env")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def fetch(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, b""
    except URLError as error:
        raise RuntimeError("La conexión con FIRMS falló") from error


def main() -> None:
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    box = plan["screening_bbox"]
    area = f"{box['west']},{box['south']},{box['east']},{box['north']}"
    template = plan["request_rule"]["endpoint"]
    windows = plan["request_rule"]["windows_per_season"]
    key = read_map_key()

    jobs = [(plan["primary_source"]["data_id"], season) for season in plan["primary_source"]["seasons"]]
    jobs.append((plan["target_season_source"]["data_id"], plan["target_season_source"]["season"]))

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT.parent.mkdir(parents=True, exist_ok=True)

    entries = []
    headers = set()
    season_rows: dict[str, int] = {}
    failures = []

    for source, season in jobs:
        rows_in_season = 0
        for window in windows:
            start = f"{season}-{window['start']}"
            name = f"{source}_{start}_{window['days']}d.csv"
            target = RAW_DIR / name
            if target.exists():
                payload = target.read_bytes()
                status = "cached"
            else:
                url = template.format(key=key, source=source, west=box["west"], south=box["south"],
                                      east=box["east"], north=box["north"], days=window["days"], start=start)
                status, payload = fetch(url)
                if status != 200 or not payload:
                    failures.append({"source": source, "start": start, "http_status": status})
                    continue
                target.write_bytes(payload)
                time.sleep(PAUSE_SECONDS)
            text = payload.decode("utf-8").strip().splitlines()
            if not text:
                failures.append({"source": source, "start": start, "http_status": status, "note": "respuesta vacía"})
                continue
            headers.add(text[0].strip())
            rows_in_season += max(len(text) - 1, 0)
            entries.append(
                {
                    "source": source,
                    "season": season,
                    "window_start": start,
                    "days": window["days"],
                    "file": name,
                    "rows": max(len(text) - 1, 0),
                    "http_status": status,
                    "sha256": sha256(target),
                }
            )
        season_rows[f"{source}|{season}"] = rows_in_season
        print(f"{source} {season} filas {rows_in_season}", flush=True)

    zero_seasons = [key_ for key_, value in season_rows.items() if value == 0]

    manifest = {
        "created": date.today().isoformat(),
        "plan": {"path": str(PLAN_PATH.relative_to(ROOT)), "sha256": sha256(PLAN_PATH)},
        "screening_bbox": area,
        "credential_handling": "La clave se leyó del entorno y no aparece en este manifiesto.",
        "entries": entries,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    audit = {
        "script": Path(__file__).name,
        "executed": date.today().isoformat(),
        "requests_completed": len(entries),
        "requests_failed": len(failures),
        "failures": failures,
        "distinct_headers": sorted(headers),
        "rows_by_source_and_season": season_rows,
        "seasons_with_zero_rows": zero_seasons,
        "checks": {
            "all_requests_returned_data": not failures,
            "all_responses_share_identical_header": len(headers) == 1,
            "no_season_with_zero_rows": not zero_seasons,
        },
        "interpretation_limits": plan["interpretation_limits"],
        "outputs": [str(RAW_DIR.relative_to(ROOT))],
    }
    audit["gate_passed"] = all(audit["checks"].values())
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: audit[k] for k in ("requests_completed", "requests_failed", "checks", "gate_passed")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
