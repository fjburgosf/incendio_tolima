"""Adjudicate the failed full-day audit against the actual required cutoff."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import eccodes
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "raw" / "era5_land" / "era5_land_core_hourly_2026-09-01_tolima_screening_bbox.grib"
FAILED_AUDIT = ROOT / "results" / "exploratory" / "audit_era5_land_core_2026-09-01.json"
OUTPUT = ROOT / "results" / "exploratory" / "adjudicate_era5_land_core_2026-09-01_required_window.json"
REQUIRED_TIMES = {datetime(2026, 9, 1, hour) for hour in range(19)}
EXPECTED = {
    "2t": ("K", "instant"),
    "2d": ("K", "instant"),
    "10u": ("m s**-1", "instant"),
    "10v": ("m s**-1", "instant"),
    "tp": ("m", "accum"),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {OUTPUT}")
    failed = json.loads(FAILED_AUDIT.read_text(encoding="utf-8"))
    observed = defaultdict(set)
    metadata = defaultdict(lambda: {"units": set(), "step_types": set(), "step_ranges": set(), "nonfinite": 0, "negative": 0})
    with SOURCE.open("rb") as stream:
        while True:
            message = eccodes.codes_grib_new_from_file(stream)
            if message is None:
                break
            try:
                short = eccodes.codes_get(message, "shortName")
                date = str(eccodes.codes_get(message, "validityDate"))
                hour = int(eccodes.codes_get(message, "validityTime"))
                validity = datetime.strptime(f"{date}{hour:04d}", "%Y%m%d%H%M")
                values = eccodes.codes_get_array(message, "values")
                observed[short].add(validity)
                metadata[short]["units"].add(eccodes.codes_get(message, "units"))
                metadata[short]["step_types"].add(eccodes.codes_get(message, "stepType"))
                metadata[short]["step_ranges"].add(str(eccodes.codes_get(message, "stepRange")))
                metadata[short]["nonfinite"] += int(np.count_nonzero(~np.isfinite(values)))
                metadata[short]["negative"] += int(np.count_nonzero(values < 0))
            finally:
                eccodes.codes_release(message)

    records = {}
    gates = {}
    for short, (unit, step_type) in EXPECTED.items():
        missing_required = sorted(REQUIRED_TIMES - observed[short])
        gates[short] = (
            not missing_required
            and metadata[short]["units"] == {unit}
            and metadata[short]["step_types"] == {step_type}
            and metadata[short]["nonfinite"] == 0
            and (short != "tp" or metadata[short]["negative"] == 0)
        )
        records[short] = {
            "missing_required_times": [v.isoformat() for v in missing_required],
            "observed_required_hour_count": len(REQUIRED_TIMES & observed[short]),
            "observed_total_hour_count": len(observed[short]),
            "units": sorted(metadata[short]["units"]),
            "step_types": sorted(metadata[short]["step_types"]),
            "nonfinite_value_count": metadata[short]["nonfinite"],
            "negative_value_count": metadata[short]["negative"],
        }
    result = {
        "audit_date": "2026-09-06",
        "source_file": str(SOURCE.relative_to(ROOT)),
        "source_sha256": sha256(SOURCE),
        "preserved_failed_full_day_audit": str(FAILED_AUDIT.relative_to(ROOT)),
        "failed_full_day_gate": not failed["all_gates_passed"],
        "diagnosed_missing_tail_utc": ["2026-09-01T22:00:00", "2026-09-01T23:00:00"],
        "required_cutoff_reason": "The latest primary event begins at 2026-08-31 18:34 UTC. Twenty four hourly validity times after that start end at 2026-09-01 18:00 UTC.",
        "required_times": [v.isoformat() for v in sorted(REQUIRED_TIMES)],
        "variables": records,
        "variable_gates": gates,
        "required_window_gate_passed": all(gates.values()),
        "interpretation_rule": "Hours 22 and 23 remain unavailable in this file and are not relabelled as present. They lie outside every frozen primary 24 h window.",
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"variable_gates": gates, "required_window_gate_passed": result["required_window_gate_passed"]}, indent=2))
    if not result["required_window_gate_passed"]:
        raise RuntimeError("Required ERA5 Land window gate failed")


if __name__ == "__main__":
    main()
