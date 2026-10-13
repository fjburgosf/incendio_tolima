# incendio_tolima

Reproducible analysis of vegetation fire activity in **Tolima, Colombia, during August 2026**, placed in historical and regional context. The pipeline compares VIIRS Suomi NPP thermal detections (FIRMS) for Tolima and seven neighboring departments over the August seasons 2012–2026 with the August atmospheric demand climatology from ERA5-Land (1991–2026), antecedent May–July precipitation from CHIRPS v3, and MapBiomas land cover. It includes a negative binomial department by season panel validated by leaving one season out, a matched spatial case control model within Tolima municipalities, and a daily association between vapor pressure deficit and thermal activity tested with circular shift permutations.

> This repository contains **only the code** and the analysis plans. No datasets, figures, tables, manuscript files or manuscript assembly scripts are included (see `.gitignore`). A thermal detection is a satellite observation of an active hot spot. It is not a fire event, an ignition or a burned area.

## Repository layout

```
incendio_tolima/
├── README.md
├── .gitignore
├── requirements.txt
├── configs/        # analysis plans with acceptance criteria fixed before execution
└── scripts/        # numbered scripts, run in numerical order
```

Scripts are grouped by numbering.

| Range | Content |
|---|---|
| `01`–`99` | Source audits. Boundaries, FIRMS, CHIRPS, IDEAM stations, Sentinel-2 (CDSE), ERA5-Land, RUNAP, Copernicus DEM GLO-90, agricultural statistics, MapBiomas, GWIS burned area, INVIAS roads, UPRA agricultural frontier, and DOI checks of the bibliography |
| `100`–`137` | Event level analysis of an earlier design and audits of historical burned area and weather sources, kept for traceability |
| `138`–`148` | August ERA5-Land climatology 1991–2026 and the regional land cover panel |
| `149`–`152` | FIRMS availability, regional VIIRS series 2012–2026, department by season activity metrics, activity versus atmospheric demand |
| `153`–`161` | Antecedent ERA5-Land and CHIRPS features, preseason panel and leave one season out prediction |
| `162`–`163` | Matched spatial case control panel and conditional logistic models for Tolima |
| `164`–`171` | Tables, figures at 600 dpi, daily demand and activity association, table value checks and Spanish versions of the figures |
| `172`–`175` | Atmospheric demand at fire cells, IDEAM station check, ENSO indices (ONI, RONI) and the ENSO amendment of the regional model |
| `176`–`178` | MapBiomas annual coverage rasters, prior land cover and protected areas under each detection, supplementary summaries |
| `179`–`186` | Robustness of the 2026 ranking, forest edges and elevation, Sentinel-2 dNBR corroboration of forest cells, prediction intervals, nested model selection, precipitation windows, ERA5-Land soil moisture and a cover adjusted spatial model |
| `187`–`189` | Missing days checked against NOAA-20 and NOAA-21, forest edge and elevation detection rates per unit area, spatial block bootstrap, Sentinel-2 corroboration version 2 with explicit dates and a forest reference, and the forest figure |

Most scripts write a JSON audit with input hashes to `results/`. Paths are relative to the repository root, under `data/raw/`, `data/interim/`, `data/processed/`, `results/` and `figures/`.

## Requirements

- Python 3.12 (Windows or Linux).

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate
pip install -r requirements.txt
```

## External data access

Credentials are read from environment variables or from a local `.env` file in the repository root, which is never tracked. No script prints or stores them.

| Source | Variable | Credential |
|---|---|---|
| NASA FIRMS | VIIRS Suomi NPP detections (standard and near real time) | `FIRMS_MAP_KEY` |
| Copernicus Climate Data Store | ERA5-Land hourly data | `CDS_API_URL`, `CDS_API_KEY` |
| Copernicus Data Space Ecosystem | Sentinel-2, DEM GLO-90 | `CDSE_CLIENT_ID`, `CDSE_CLIENT_SECRET` |
| NASA Earthdata | MODIS/VIIRS burned area | token entered at a no-echo prompt |
| NOAA CPC, IDEAM open data (datos.gov.co), MapBiomas coverage rasters | ENSO indices, station temperature and humidity, annual land cover | public download |
| CHIRPS v3, MapBiomas Colombia, IDEAM, geoBoundaries | precipitation, land cover, stations, boundaries | public download |

## Execution order

Run from the repository root. For the current analysis:

```bash
python scripts/138_download_era5_land_august_climatology_source.py
python scripts/147_build_regional_landcover_panel.py
python scripts/148_build_august_fire_danger_climatology.py
python scripts/150_download_regional_fire_activity_series.py
python scripts/151_build_department_season_activity_metrics.py
python scripts/152_evaluate_activity_danger_decoupling.py
python scripts/157_download_chirps_monthly_preseason_source.py
python scripts/158_construct_chirps_regional_antecedent_features.py
python scripts/159_build_regional_preseason_prediction_panel.py
python scripts/160_evaluate_regional_preseason_prediction.py
python scripts/161_summarize_regional_preseason_prediction.py
python scripts/162_build_spatial_case_control_panel.py
python scripts/163_fit_tolima_spatial_case_control_model.py
python scripts/168_daily_vpd_activity_association.py
python scripts/164_generate_revision_v2_figures.py
python scripts/165_generate_revision_v2_tables.py
python scripts/166_generate_core_manuscript_figures.py
python scripts/169_generate_daily_vpd_figure.py
# forest, ENSO and robustness analyses
python scripts/172_fire_location_atmospheric_demand.py
python scripts/174_download_enso_indices.py
python scripts/175_evaluate_enso_preseason_amendment.py
python scripts/176_download_mapbiomas_coverage_rasters.py
python scripts/177_forest_cover_at_detections.py
python scripts/178_supplementary_cover_activity_and_outage_counts.py
python scripts/173_ideam_station_demand_check.py
python scripts/179_review_robustness_detections_forest_station.py
python scripts/180_review_prediction_intervals_and_tolima_specifications.py
python scripts/181_sentinel2_forest_detection_corroboration.py
python scripts/182_summarize_sentinel2_forest_corroboration.py
python scripts/183_review_nested_selection_rain_windows.py
python scripts/184_era5_land_soil_moisture_antecedent.py
python scripts/185_cover_adjusted_spatial_case_control.py
python scripts/186_diagnose_soil_moisture_rain_discrepancy.py
python scripts/187_review_v3_missing_days_area_rates_blocks.py
python scripts/188_sentinel2_forest_corroboration_v2.py
python scripts/189_generate_forest_figure.py
```

These scripts also read raw inputs obtained during the source audits (scripts `01`–`99`), such as MapBiomas statistics, the CORTOLIMA municipal boundaries and the Copernicus DEM GLO-90 tiles. The geoBoundaries ADM1 layer for Colombia is expected at `data/raw/boundaries/geoBoundaries-COL-ADM1.geojson`. Random steps use fixed seeds declared in the scripts or in `configs/`.

## Citation

Code is provided for reproducibility of the analysis. Please cite the associated manuscript (in preparation) if you reuse the pipeline.
