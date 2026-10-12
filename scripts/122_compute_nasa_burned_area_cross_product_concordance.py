"""Compute pre-specified within-Tolima concordance between historical burn products.

The two products are compared as alternate open products on the same grid. This
is neither an independent validation exercise nor a comparison with GWIS.
"""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import numpy as np
from pyproj import CRS


ROOT = Path(__file__).resolve().parents[1]
ACCESS_DATE = "2026-09-07"
PLAN_SOURCE = ROOT / "configs" / "nasa_burned_area_pixel_qc_plan_v1.json"
GEORF_AUDIT = ROOT / "results" / "exploratory" / f"audit_nasa_burned_area_hdf_georeference_pixel_qc_{ACCESS_DATE}_v2.json"
OUTPUT = ROOT / "results" / "exploratory" / f"nasa_burned_area_cross_product_concordance_{ACCESS_DATE}.json"
SUMMARY = ROOT / "data" / "interim" / "nasa_burned_area_historical_cross_product_concordance_v1.csv"


def load_georeference_module() -> object:
    source = ROOT / "scripts" / "121_audit_nasa_burned_area_hdf_georeference_pixel_qc.py"
    specification = importlib.util.spec_from_file_location("nasa_pixel_qc", source)
    if specification is None or specification.loader is None:
        raise RuntimeError("Could not load the preceding georeferencing audit module")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def grid_fingerprint(metadata: dict[str, object]) -> tuple[object, ...]:
    return (
        metadata["x_dimension"],
        metadata["y_dimension"],
        tuple(metadata["upper_left_m"]),
        tuple(metadata["lower_right_m"]),
        metadata["projection"],
        metadata["sphere_radius_m"],
        metadata["grid_origin"],
    )


def primary_mask(layers: dict[str, np.ndarray], geographic_mask: np.ndarray) -> np.ndarray:
    burn_date = layers["Burn Date"]
    qa = layers["QA"].astype(np.uint8, copy=False)
    return geographic_mask & (burn_date > 0) & ((qa & 1) == 1) & (((qa >> 1) & 1) == 1)


def statistics(
    mcd_layers: dict[str, np.ndarray],
    vnp_layers: dict[str, np.ndarray],
    mcd_mask: np.ndarray,
    vnp_mask: np.ndarray,
) -> dict[str, object]:
    intersection = mcd_mask & vnp_mask
    union = mcd_mask | vnp_mask
    intersection_count = int(np.count_nonzero(intersection))
    union_count = int(np.count_nonzero(union))
    result: dict[str, object] = {
        "mcd_primary_burned_pixel_count": int(np.count_nonzero(mcd_mask)),
        "vnp_primary_burned_pixel_count": int(np.count_nonzero(vnp_mask)),
        "intersection_pixel_count": intersection_count,
        "union_pixel_count": union_count,
        "jaccard_overlap": (intersection_count / union_count) if union_count else None,
    }
    if not intersection_count:
        result.update(
            {
                "exact_ordinal_day_agreement_count": 0,
                "exact_ordinal_day_agreement_fraction": None,
                "nonnegative_uncertainty_pair_count": 0,
                "uncertainty_compatible_agreement_count": 0,
                "uncertainty_compatible_agreement_fraction": None,
                "maximum_absolute_ordinal_day_difference": None,
            }
        )
        return result
    mcd_day = mcd_layers["Burn Date"][intersection].astype(np.int16, copy=False)
    vnp_day = vnp_layers["Burn Date"][intersection].astype(np.int16, copy=False)
    differences = np.abs(mcd_day - vnp_day)
    mcd_uncertainty = mcd_layers["Burn Date Uncertainty"][intersection].astype(np.int16, copy=False)
    vnp_uncertainty = vnp_layers["Burn Date Uncertainty"][intersection].astype(np.int16, copy=False)
    nonnegative_uncertainty = (mcd_uncertainty >= 0) & (vnp_uncertainty >= 0)
    compatible = nonnegative_uncertainty & (differences <= (mcd_uncertainty + vnp_uncertainty))
    valid_uncertainty_count = int(np.count_nonzero(nonnegative_uncertainty))
    result.update(
        {
            "exact_ordinal_day_agreement_count": int(np.count_nonzero(differences == 0)),
            "exact_ordinal_day_agreement_fraction": float(np.count_nonzero(differences == 0) / intersection_count),
            "nonnegative_uncertainty_pair_count": valid_uncertainty_count,
            "uncertainty_compatible_agreement_count": int(np.count_nonzero(compatible)),
            "uncertainty_compatible_agreement_fraction": (
                float(np.count_nonzero(compatible) / valid_uncertainty_count) if valid_uncertainty_count else None
            ),
            "maximum_absolute_ordinal_day_difference": int(differences.max()),
        }
    )
    return result


def main() -> None:
    for target in (OUTPUT, SUMMARY):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    georeference_audit = json.loads(GEORF_AUDIT.read_text(encoding="utf-8"))
    if georeference_audit.get("acceptance_result") != "pass":
        raise RuntimeError("The required georeferencing audit did not pass")
    plan = json.loads(PLAN_SOURCE.read_text(encoding="utf-8"))
    module = load_georeference_module()
    native_crs = CRS.from_proj4("+proj=sinu +R=6371007.181 +units=m +no_defs")
    tolima_native, transform_audit = module.load_tolima_native(native_crs)
    records_by_pair = {(record["product"], record["year"]): record for record in georeference_audit["records"]}
    annual_records: list[dict[str, object]] = []
    for year in module.EXPECTED_YEARS:
        mcd_audit = records_by_pair[("MCD64A1", year)]
        vnp_audit = records_by_pair[("VNP64A1", year)]
        mcd_path = module.RAW_DIR / mcd_audit["file"]
        vnp_path = module.RAW_DIR / vnp_audit["file"]
        mcd_layers, mcd_metadata, _ = module.read_layers(mcd_path)
        vnp_layers, vnp_metadata, _ = module.read_layers(vnp_path)
        if grid_fingerprint(mcd_metadata) != grid_fingerprint(vnp_metadata):
            raise RuntimeError(f"Product grid mismatch in {year}")
        mcd_geographic_mask, mcd_grid_audit = module.target_mask(mcd_metadata, tolima_native)
        vnp_geographic_mask, vnp_grid_audit = module.target_mask(vnp_metadata, tolima_native)
        if not np.array_equal(mcd_geographic_mask, vnp_geographic_mask):
            raise RuntimeError(f"Product Tolima clip mismatch in {year}")
        mcd_primary = primary_mask(mcd_layers, mcd_geographic_mask)
        vnp_primary = primary_mask(vnp_layers, vnp_geographic_mask)
        mcd_shortened = ((mcd_layers["QA"].astype(np.uint8, copy=False) >> 2) & 1) == 1
        vnp_shortened = ((vnp_layers["QA"].astype(np.uint8, copy=False) >> 2) & 1) == 1
        sensitivity_mcd = mcd_primary & ~mcd_shortened
        sensitivity_vnp = vnp_primary & ~vnp_shortened
        primary_statistics = statistics(mcd_layers, vnp_layers, mcd_primary, vnp_primary)
        sensitivity_statistics = statistics(mcd_layers, vnp_layers, sensitivity_mcd, sensitivity_vnp)
        annual_records.append(
            {
                "year": year,
                "tolima_cell_centres": mcd_grid_audit["tolima_cell_centres"],
                "native_cell_area_square_m": mcd_grid_audit["native_cell_area_square_m"],
                "primary": primary_statistics,
                "sensitivity_excluding_shortened_mapping_period": sensitivity_statistics,
            }
        )
    fieldnames = [
        "year", "tolima_cell_centres", "mcd_primary_burned_pixel_count", "vnp_primary_burned_pixel_count",
        "intersection_pixel_count", "union_pixel_count", "jaccard_overlap", "exact_ordinal_day_agreement_count",
        "exact_ordinal_day_agreement_fraction", "nonnegative_uncertainty_pair_count",
        "uncertainty_compatible_agreement_count", "uncertainty_compatible_agreement_fraction",
        "maximum_absolute_ordinal_day_difference", "sensitivity_jaccard_overlap",
        "sensitivity_exact_ordinal_day_agreement_fraction", "sensitivity_uncertainty_compatible_agreement_fraction",
    ]
    csv_records = []
    for record in annual_records:
        primary = record["primary"]
        sensitivity = record["sensitivity_excluding_shortened_mapping_period"]
        csv_records.append(
            {
                "year": record["year"],
                "tolima_cell_centres": record["tolima_cell_centres"],
                **primary,
                "sensitivity_jaccard_overlap": sensitivity["jaccard_overlap"],
                "sensitivity_exact_ordinal_day_agreement_fraction": sensitivity["exact_ordinal_day_agreement_fraction"],
                "sensitivity_uncertainty_compatible_agreement_fraction": sensitivity["uncertainty_compatible_agreement_fraction"],
            }
        )
    with SUMMARY.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_records)
    output = {
        "audit_date": ACCESS_DATE,
        "inference_class": "descriptive cross-product concordance",
        "plan_source": str(PLAN_SOURCE.relative_to(ROOT)),
        "required_georeference_audit": str(GEORF_AUDIT.relative_to(ROOT)),
        "tolima_projection_transform_recheck": transform_audit,
        "pre_execution_acceptance_criteria": {
            "georeference_audit_passes": True,
            "product_native_grids_identical_within_each_year": True,
            "product_tolima_clip_masks_identical_within_each_year": True,
            "metric_rule": plan["cross_product_descriptive_metrics"]["execution_rule"],
        },
        "acceptance_result": "pass",
        "annual_records": annual_records,
        "summary_csv": str(SUMMARY.relative_to(ROOT)),
        "interpretation_limit": plan["cross_product_descriptive_metrics"]["language_limit"],
    }
    OUTPUT.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "summary": str(SUMMARY), "acceptance_result": "pass"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
