"""Descarga y audita la capa oficial consolidada RUNAP sin transformarla."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from urllib.request import urlopen

import shapefile


ROOT = Path(__file__).resolve().parents[1]
URL = "https://storage.googleapis.com/pnn_geodatabase/runap/latest.zip"
RAW_DIR = ROOT / "data" / "raw" / "runap"
RAW_FILE = RAW_DIR / "runap_latest.zip"
MANIFEST = RAW_DIR / "runap_latest_manifest.json"
OUTPUT = ROOT / "results" / "exploratory" / "audit_runap_latest.json"
TOLIMA_SCREENING_BBOX = (-76.2, 2.0, -74.0, 6.2)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def overlaps(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> bool:
    return not (left[2] < right[0] or left[0] > right[2] or left[3] < right[1] or left[1] > right[3])


def main() -> None:
    if RAW_FILE.exists() or MANIFEST.exists() or OUTPUT.exists():
        raise FileExistsError("La auditoría RUNAP ya existe. No se sobreescriben insumos ni resultados.")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with urlopen(URL, timeout=120) as response:
        payload = response.read()
        content_type = response.headers.get_content_type()
    RAW_FILE.write_bytes(payload)
    manifest = {
        "url": URL,
        "file": RAW_FILE.name,
        "bytes": len(payload),
        "sha256": sha256(RAW_FILE),
        "content_type": content_type,
        "purpose": "Insumo administrativo territorial. No es desenlace ni límite del incendio.",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = archive.namelist()
        shapefiles = [member for member in members if member.lower().endswith(".shp")]
        if len(shapefiles) != 1:
            raise ValueError(f"Se esperaba un solo SHP y se encontraron {len(shapefiles)}")
        stem = Path(shapefiles[0]).with_suffix("").as_posix()
        with archive.open(f"{stem}.shp") as shp, archive.open(f"{stem}.shx") as shx, archive.open(f"{stem}.dbf") as dbf:
            reader = shapefile.Reader(shp=io.BytesIO(shp.read()), shx=io.BytesIO(shx.read()), dbf=io.BytesIO(dbf.read()))
            field_names = [field[0] for field in reader.fields[1:]]
            candidate_records = []
            for index, shape_record in enumerate(reader.iterShapeRecords()):
                bbox = tuple(float(value) for value in shape_record.shape.bbox)
                if overlaps(bbox, TOLIMA_SCREENING_BBOX):
                    attributes = dict(zip(field_names, shape_record.record))
                    candidate_records.append({
                        "record_index": index,
                        "bbox": bbox,
                        "attributes": attributes,
                    })
            audit = {
                "source_file": str(RAW_FILE.relative_to(ROOT)).replace("\\", "/"),
                "zip_members": members,
                "shapefile": shapefiles[0],
                "shape_type": reader.shapeTypeName,
                "feature_count": len(reader),
                "fields": field_names,
                "dataset_bbox": tuple(float(value) for value in reader.bbox),
                "screening_bbox": TOLIMA_SCREENING_BBOX,
                "screening_candidate_count": len(candidate_records),
                "screening_candidates": candidate_records,
                "interpretation_limit": (
                    "Los candidatos se obtienen por superposición de cajas envolventes con un rectángulo de cribado. "
                    "No es una intersección exacta con Tolima, ni prueba exposición, protección efectiva o causalidad."
                ),
            }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "feature_count": audit["feature_count"],
        "shape_type": audit["shape_type"],
        "field_count": len(audit["fields"]),
        "screening_candidate_count": audit["screening_candidate_count"],
        "raw_sha256": manifest["sha256"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
