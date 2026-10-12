"""Revisa las excepciones nominales EVA con la tabla administrativa ya verificada."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "results" / "exploratory" / "audit_eva_cortolima_municipal_linkage.json"
CROSSWALK = ROOT / "configs" / "ideam_cortolima_municipality_crosswalk.json"
OUTPUT = ROOT / "results" / "exploratory" / "audit_eva_cortolima_municipal_linkage_crosswalk.json"


def normalized(value: object) -> str:
    value = "" if value is None else str(value)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]+", "", value.upper())


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError("La revisión de enlace EVA ya existe. No se sobreescriben resultados.")
    audit = json.loads(INPUT.read_text(encoding="utf-8"))
    crosswalk = json.loads(CROSSWALK.read_text(encoding="utf-8"))["mappings"]
    sheets = []
    for item in audit["sheets"]:
        names = item["unresolved_eva_municipalities"]
        resolved = [name for name in names if normalized(name) in crosswalk]
        unresolved = [name for name in names if normalized(name) not in crosswalk]
        sheets.append({
            "source": item["source"],
            "sheet": item["sheet"],
            "direct_normalized_match_count": item["unique_direct_normalized_match_count"],
            "crosswalk_match_count": len(resolved),
            "crosswalk_eva_municipalities": resolved,
            "unresolved_eva_municipalities": unresolved,
        })
    result = {
        "input_audit": str(INPUT.relative_to(ROOT)).replace("\\", "/"),
        "crosswalk_source": str(CROSSWALK.relative_to(ROOT)).replace("\\", "/"),
        "sheets": sheets,
        "interpretation_limit": (
            "Esta revisión solo resuelve dos denominaciones administrativas contra una tabla explícita. "
            "No valida códigos DANE ni transforma el enlace municipal en un enlace individual o causal."
        ),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
