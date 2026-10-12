"""Verifica la estructura elemental del GRIB de auditoría ERA5 Land."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "raw" / "era5_land" / "era5_land_audit_2m_temperature_2026-08-20T00.grib"
OUTPUT = ROOT / "results" / "exploratory" / "audit_era5_land_grib.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def main() -> None:
    content = SOURCE.read_bytes()
    if len(content) < 20 or content[:4] != b"GRIB":
        raise ValueError("El archivo no inicia con el indicador GRIB")
    edition = content[7]
    if edition == 1:
        total_length = int.from_bytes(content[4:7], byteorder="big")
        discipline = None
    elif edition == 2:
        total_length = int.from_bytes(content[8:16], byteorder="big")
        discipline = content[6]
    else:
        raise ValueError(f"Edición GRIB no contemplada: {edition}")
    audit = {
        "source_file": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
        "bytes": len(content),
        "sha256": sha256(SOURCE),
        "magic": content[:4].decode("ascii"),
        "discipline": discipline,
        "edition": edition,
        "declared_message_length": total_length,
        "declared_length_matches_file": total_length == len(content),
        "trailer": content[-4:].decode("ascii", errors="replace"),
        "trailer_is_valid": content[-4:] == b"7777",
        "interpretation_limit": (
            "Esta comprobación verifica contenedor GRIB. No decodifica valores, unidades, "
            "coordenadas, variable física ni calidad del reanálisis."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
