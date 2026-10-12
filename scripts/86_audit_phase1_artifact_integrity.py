"""Audit syntax, structured files, and recorded SHA256 links for Phase 1.

This verifies artifact integrity only. It does not revalidate scientific
meaning or promote exploratory outputs to study results.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "results" / "exploratory" / "audit_phase1_artifact_integrity_v7.json"
SUPERSEDED_OUTPUT = ROOT / "results" / "exploratory" / "audit_phase1_artifact_integrity_v6.json"
JSON_ROOTS = [ROOT / "results" / "exploratory", ROOT / "data" / "interim", ROOT / "configs"]
CSV_ROOTS = [ROOT / "data" / "interim"]
HASH_JSON_ROOTS = [ROOT / "results" / "exploratory", ROOT / "data" / "raw"]
HEX64 = re.compile(r"^[0-9A-Fa-f]{64}$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def relative(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def resolve_recorded_file(reference: str, record_path: Path) -> Path | None:
    reference_path = Path(reference.replace("\\", "/"))
    candidates = [ROOT / reference_path, record_path.parent / reference_path]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    basename_matches = list((ROOT / "data" / "raw").rglob(reference_path.name))
    if len(basename_matches) == 1:
        return basename_matches[0].resolve()
    return None


def extract_hash_pairs(value, record_path: Path, location: str = "$") -> list[dict]:
    pairs = []
    if isinstance(value, dict):
        if isinstance(value.get("file"), str) and isinstance(value.get("sha256"), str) and HEX64.fullmatch(value["sha256"]):
            resolved = resolve_recorded_file(value["file"], record_path)
            pairs.append({
                "record": relative(record_path), "location": location,
                "file_reference": value["file"], "recorded_sha256": value["sha256"].upper(),
                "resolved_file": relative(resolved) if resolved and ROOT in resolved.parents else None,
                "resolution_status": "resolved" if resolved else "missing",
            })
        for key, child in value.items():
            pairs.extend(extract_hash_pairs(child, record_path, f"{location}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            pairs.extend(extract_hash_pairs(child, record_path, f"{location}[{index}]"))
    return pairs


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {OUTPUT}")

    script_checks = []
    for path in sorted((ROOT / "scripts").glob("*.py")):
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
            error = None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        script_checks.append({"file": relative(path), "syntax_ok": error is None, "error": error})

    json_checks, loaded_records = [], {}
    for base in JSON_ROOTS:
        for path in sorted(set(base.rglob("*.json")) | set(base.rglob("*.geojson"))):
            if path.resolve() == OUTPUT.resolve():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
                error = None
                loaded_records[path.resolve()] = value
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            json_checks.append({"file": relative(path), "json_ok": error is None, "error": error})

    csv_checks = []
    for base in CSV_ROOTS:
        for path in sorted(base.rglob("*.csv")):
            try:
                with path.open(encoding="utf-8-sig", newline="") as handle:
                    reader = csv.reader(handle)
                    header = next(reader)
                    row_count = sum(1 for _ in reader)
                error = None
            except Exception as exc:
                header, row_count = [], None
                error = f"{type(exc).__name__}: {exc}"
            csv_checks.append({"file": relative(path), "csv_ok": error is None,
                               "column_count": len(header), "data_row_count": row_count, "error": error})

    hash_record_paths = set()
    for base in HASH_JSON_ROOTS:
        hash_record_paths.update(base.rglob("*.json"))
    pairs = []
    for path in sorted(hash_record_paths):
        try:
            value = loaded_records.get(path.resolve())
            if value is None:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
            pairs.extend(extract_hash_pairs(value, path))
        except Exception:
            continue
    hash_cache = {}
    for pair in pairs:
        if pair["resolution_status"] != "resolved":
            pair["hash_matches"] = None
            continue
        path = (ROOT / pair["resolved_file"]).resolve()
        if path not in hash_cache:
            hash_cache[path] = sha256(path)
        pair["computed_sha256"] = hash_cache[path]
        pair["hash_matches"] = hash_cache[path] == pair["recorded_sha256"]

    summary = {
        "python_script_count": len(script_checks),
        "python_syntax_failures": sum(not item["syntax_ok"] for item in script_checks),
        "structured_json_count": len(json_checks),
        "json_parse_failures": sum(not item["json_ok"] for item in json_checks),
        "interim_csv_count": len(csv_checks),
        "csv_parse_failures": sum(not item["csv_ok"] for item in csv_checks),
        "recorded_file_hash_pairs": len(pairs),
        "missing_recorded_files": sum(item["resolution_status"] == "missing" for item in pairs),
        "resolved_hash_mismatches": sum(item.get("hash_matches") is False for item in pairs),
    }
    summary["integrity_gate_passed"] = all(summary[key] == 0 for key in (
        "python_syntax_failures", "json_parse_failures", "csv_parse_failures",
        "missing_recorded_files", "resolved_hash_mismatches",
    ))
    output = {
        "audit_date": "2026-09-07", "inference_class": "reproducibility artifact integrity audit",
        "supersedes": {
            "output": relative(SUPERSEDED_OUTPUT),
            "reason": "Refresh after adding authenticated historical NASA raw-data audits. The resolver and acceptance criteria are unchanged from version 6.",
            "scientific_criteria_changed": False,
        },
        "acceptance_rule_fixed_before_execution": "No syntax failures, JSON or CSV parse failures, missing files in recognized file plus SHA256 records, or resolved SHA256 mismatches.",
        "summary": summary, "python_scripts": script_checks, "structured_json": json_checks,
        "interim_csv": csv_checks, "recorded_hash_links": pairs,
        "interpretation_limit": "Passing confirms internal file readability and recorded hash links at this checkout. It does not validate source authority, variable meaning, linkage validity, statistical design, or scientific conclusions.",
    }
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
