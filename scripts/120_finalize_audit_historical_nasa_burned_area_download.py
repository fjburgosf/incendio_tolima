"""Verify and finalize the authenticated historical NASA burned-area download."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pyhdf.SD import SD, SDC


ROOT = Path(__file__).resolve().parents[1]
ACCESS_DATE = "2026-09-07"
PARTIAL_DIR = ROOT / "data" / "raw" / "nasa" / f".burned_area_historical_{ACCESS_DATE}.partial"
RAW_DIR = ROOT / "data" / "raw" / "nasa" / f"burned_area_historical_{ACCESS_DATE}"
OUTPUT = ROOT / "results" / "exploratory" / f"audit_historical_nasa_burned_area_download_{ACCESS_DATE}.json"
TARGET_YEARS = {2018, 2020, 2022, 2024, 2025}
TARGET_PRODUCTS = {"MCD64A1", "VNP64A1"}
REQUIRED_LAYERS = {"Burn Date", "Burn Date Uncertainty", "QA", "First Day", "Last Day"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def hdf_layers(path: Path) -> list[str]:
    handle = SD(str(path), SDC.READ)
    try:
        return sorted(handle.datasets())
    finally:
        handle.end()


def main() -> None:
    for target in (RAW_DIR, OUTPUT):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing path: {target}")
    manifests = sorted(PARTIAL_DIR.glob("manifest_run_*.json"))
    if not manifests:
        raise RuntimeError("No partial manifests found")
    entries = []
    for manifest_path in manifests:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries.extend(payload["downloaded_in_this_run"])
    key_counts: dict[tuple[str, int], int] = {}
    file_checks = []
    for entry in entries:
        key = (str(entry["product"]), int(entry["year"]))
        key_counts[key] = key_counts.get(key, 0) + 1
        path = PARTIAL_DIR / str(entry["file"])
        computed_sha = sha256(path) if path.exists() else None
        layers = hdf_layers(path) if path.exists() else []
        file_checks.append(
            {
                "product": entry["product"],
                "year": entry["year"],
                "granule_id": entry["granule_id"],
                "file": entry["file"],
                "file_exists": path.exists(),
                "recorded_sha256": entry["sha256"],
                "computed_sha256": computed_sha,
                "sha256_matches": computed_sha == entry["sha256"],
                "required_layers_present": REQUIRED_LAYERS.issubset(layers),
                "observed_layers": layers,
            }
        )
    expected_keys = {(product, year) for product in TARGET_PRODUCTS for year in TARGET_YEARS}
    observed_keys = set(key_counts)
    acceptance = {
        "all_expected_product_year_pairs_present_once": observed_keys == expected_keys and all(count == 1 for count in key_counts.values()),
        "all_files_exist": all(item["file_exists"] for item in file_checks),
        "all_hashes_match": all(item["sha256_matches"] for item in file_checks),
        "all_required_layers_present": all(item["required_layers_present"] for item in file_checks),
    }
    if not all(acceptance.values()):
        raise RuntimeError(f"Finalization acceptance checks failed: {acceptance}")
    final_manifest = {
        "access_date": ACCESS_DATE,
        "entries": entries,
        "credential_handling": "Earthdata token was supplied through no-echo prompts and is not stored.",
        "finalization_checks": acceptance,
    }
    (PARTIAL_DIR / "download_manifest.json").write_text(
        json.dumps(final_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    PARTIAL_DIR.rename(RAW_DIR)
    audit = {
        "audit_date": ACCESS_DATE,
        "inference_class": "descriptive raw-file audit",
        "source": {
            "raw_directory": str(RAW_DIR.relative_to(ROOT)),
            "download_manifest_sha256": sha256(RAW_DIR / "download_manifest.json"),
        },
        "acceptance_rule_fixed_before_execution": "Every selected product-year pair is present exactly once, raw file hashes match, and each HDF exposes Burn Date, Burn Date Uncertainty, QA, First Day and Last Day.",
        "acceptance": acceptance,
        "files": file_checks,
        "interpretation": "Passing proves file identity and layer availability. It does not prove pixel quality, burned-area validity, event matching or independent validation of GWIS.",
    }
    OUTPUT.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"acceptance": acceptance, "file_count": len(file_checks)}, indent=2))


if __name__ == "__main__":
    main()
