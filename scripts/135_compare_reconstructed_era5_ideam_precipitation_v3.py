"""Describe nearest-cell precipitation agreement after the accepted reconstruction."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "132_compare_reconstructed_era5_ideam_precipitation.py"


if __name__ == "__main__":
    specification = importlib.util.spec_from_file_location("era5_ideam_v2_module", SOURCE)
    if specification is None or specification.loader is None:
        raise RuntimeError("Unable to load the shared gauge-comparison implementation")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    module.PLAN = ROOT / "configs" / "precipitation_reconstruction_and_exploratory_analysis_plan_v2.json"
    module.GRID = ROOT / "data" / "interim" / "era5_land_hourly_precipitation_increment_v3.npz"
    module.DAILY_OUT = ROOT / "results" / "exploratory" / "era5_ideam_daily_precipitation_comparison_v3.csv"
    module.MONTHLY_OUT = ROOT / "results" / "exploratory" / "era5_ideam_monthly_precipitation_comparison_v3.csv"
    module.AUDIT = ROOT / "results" / "exploratory" / "audit_era5_ideam_precipitation_comparison_v3.json"
    module.main()
