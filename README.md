# Crop Residue Pipeline (Dalarna)

This repository contains a Sentinel-1 processing pipeline for field-level crop residue analysis in Dalarna.

Overview:
- Search & download Sentinel-1 SLC products
- Process backscatter (Gamma0 in dB)
- Process interferometric coherence
- Initialize per-field dataset (masks, aligned grids, parquet metadata)
- Extract per-field rasters for analysis


## Repository Overview

- `1_Sentinel-1_scenes_download.py`
	- Searches Copernicus for Sentinel-1 SLC scenes over an ROI.
	- Selects a best coherence pair (shortest temporal baseline in grouped orbit tracks).
	- Downloads selected scenes.

- `2_process_backscatter_by_subswath.py`
	- Runs SNAP GPT graph for SLC backscatter processing.
	- Produces terrain-corrected Gamma0 backscatter in dB as GeoTIFF.

- `3_process_coherence_by_subswath.py`
	- Runs SNAP GPT graph for two-scene coherence processing.
	- Produces terrain-corrected coherence GeoTIFF.

- `4_initialize_dataset.py`
	- Builds field-level folder structure and auxiliary files:
		- field polygon parquet
		- aligned bounding-box parquet
		- pixel mask GeoTIFF

- `5_field_extraction.py`
	- Reprojects Sentinel rasters to each field mask grid.
	- Crops/masks data into field-specific backscatter and coherence subfolders.

- `convert_to_parquet.py`
	- Small one-off helper for converting a shapefile to parquet.
	- Contains hardcoded paths and is not required if your fields file is already parquet.


## Models

The `models/` directory contains model definitions and helper utilities used for downstream analysis and experiments. These modules are optional for running the Sentinel-1 processing pipeline but useful if you plan to train or run ML models on the extracted field data.

- `cnnencodernet.py`: convolutional encoder network (pixel/tile inputs).
- `pixelsetencodernet.py`: encoder for pixel-set style inputs.
- `temporal_encoder.py`: temporal encoding utilities and recurrent/transformer-based components.
- `model_manager.py`: training, saving, and model loading helpers.

See [models/README.md](models/README.md) for more details and examples on training and inference.


## Prerequisites

### 1. Python Environment

The repository includes `environment.yml` for Conda. Use the Conda environment (recommended):

```powershell
conda env create -f environment.yml
conda activate crop_residue_dalarna
```

### 2. SNAP GPT

Backscatter and coherence scripts call `gpt` directly (SNAP Graph Processing Tool).

Requirements:
- Install ESA SNAP (with Sentinel-1 toolbox components).
- Ensure `gpt` is available in your system PATH.
- Verify with:

```powershell
gpt -h
```

### 3. Credentials / Access for Download Step

`1_Sentinel-1_scenes_download.py` uses `phidown` and Copernicus search/download APIs.

Make sure your environment is configured so `phidown` can authenticate.


## Expected Input Data

- ROI shapefile for scene search (`--roi`) in step 1.
- Field polygons parquet for dataset initialization (`--fields_file`) in step 4.
	- If your source is shapefile, convert to parquet first (either with GeoPandas or the helper script).

Note: this repo contains a small helper `convert_to_parquet.py` intended for quick local conversions; it contains hardcoded paths and is meant as an example rather than a production utility.


## Recommended Folder Layout

You can use any layout, but this is a practical structure:

```text
project_root/
	data/
		downloads/            # downloaded .SAFE or .zip Sentinel-1 products
		backscatter_output/   # outputs from step 2
		coherence_output/     # outputs from step 3
		dataset/              # outputs from steps 4-5
		fields/
			subparcels.parquet
```


## Pipeline: Step-by-Step

### Step 1: Search and Download Sentinel-1 SLC Pair

Script: `1_Sentinel-1_scenes_download.py`

What it does:
- Loads ROI and simplifies geometry (falls back to bounding box if WKT gets too long).
- Queries Sentinel-1 SLC products in your date range.
- Parses product metadata from product name.
- Filters to S1A.
- Groups by relative orbit + orbit direction.
- Picks the best two-date pair with shortest temporal baseline.
- Downloads the selected pair.

Run:

```powershell
python 1_Sentinel-1_scenes_download.py \
	--roi data/roi/your_roi.shp \
	--output-dir data/downloads \
	--start-date 2024-09-18T00:00:00 \
	--end-date 2024-09-30T23:59:59
```


### Step 2: Process Backscatter (Gamma0 dB)

Script: `2_process_backscatter_by_subswath.py`

What it does (SNAP chain):
1. Read SLC
2. Apply orbit file
3. Thermal noise removal
4. Calibration to Gamma0 (linear)
5. TOPSAR Split (selected subswath)
6. TOPSAR Deburst
7. Speckle filtering (Lee Sigma)
8. Terrain correction with radiometric normalization
9. Convert linear to dB
10. Optional subset to ROI
11. Write GeoTIFF-BigTIFF

Run:

```powershell
python 2_process_backscatter_by_subswath.py \
	--input-dir data/downloads \
	--output-dir data/backscatter_output \
	--subswath IW3 \
	--tile-cache 2048M \
	--threads 4
```

Optional ROI subset:

```powershell
python 2_process_backscatter_by_subswath.py \
	--input-dir data/downloads \
	--output-dir data/backscatter_output \
	--subswath IW3 \
	--roi-shapefile data/roi/your_roi.shp
```

Output:
- GeoTIFF files in `data/backscatter_output/backscatter`


### Step 3: Process Coherence

Script: `3_process_coherence_by_subswath.py`

What it does:
- Finds products in input directory.
- Builds adjacent pairs.
- Keeps only same-track pairs (time-of-day tolerance).
- Keeps only pairs within max temporal baseline (12 days).
- Runs SNAP coherence graph and writes GeoTIFF output.

Run:

```powershell
python 3_process_coherence_by_subswath.py \
	--input-dir data/downloads \
	--output-dir data/coherence_output \
	--subswath IW3 \
	--tile-cache 2048M \
	--threads 4
```

Output:
- GeoTIFF files in `data/coherence_output/coherence`


### Step 4: Initialize Field Dataset Structure

Script: `4_initialize_dataset.py`

What it does per field:
- Creates folder under `dataset_path/data/<field_id>`
- Saves original field polygon as parquet
- Creates grid-aligned bounding box (10 m grid) and saves parquet
- Builds binary mask raster (`mask_<field_id>.tif`) where cell is 1 only if fully contained in polygon

Run:

```powershell
python 4_initialize_dataset.py \
	--fields_file data/fields/subparcels_dalarna.parquet \
	--dataset_path data/dataset \
	--id_field <your_id_column> \
	--crs EPSG:3006
```

Notes:
- If `--id_field` is omitted, ids default to `field0`, `field1`, ...
- Grid size is currently fixed to 10 m in script.


### Step 5: Extract Field-Level Backscatter and Coherence

Script: `5_field_extraction.py`

What it does:
- Reads each field mask created in step 4.
- For every backscatter/coherence raster:
	- reprojects raster to the field mask grid
	- checks full field coverage
	- applies mask (outside-field pixels set to nodata)
	- writes per-field outputs

Run:

```powershell
python 5_field_extraction.py \
	--datasetpath data/dataset \
	--backscatterdir data/backscatter_output/backscatter \
	--coherencedir data/coherence_output/coherence
```

Outputs:
- `data/dataset/data/<field_id>/backscatter/*.tif`
- `data/dataset/data/<field_id>/coherence/*.tif`


## End-to-End Example

```powershell
# 1) Download scene pair
python 1_Sentinel-1_scenes_download.py --roi data/roi/your_roi.shp --output-dir data/downloads

# 2) Backscatter
python 2_process_backscatter_by_subswath.py --input-dir data/downloads --output-dir data/backscatter_output --subswath IW3

# 3) Coherence
python 3_process_coherence_by_subswath.py --input-dir data/downloads --output-dir data/coherence_output --subswath IW3

# 4) Field dataset initialization
python 4_initialize_dataset.py --fields_file data/fields/subparcels_dalarna.parquet --dataset_path data/dataset --id_field FIELD_ID

# 5) Field extraction
python 5_field_extraction.py --datasetpath data/dataset --backscatterdir data/backscatter_output/backscatter --coherencedir data/coherence_output/coherence
```


## Performance Tuning

Both SNAP processing scripts support:
- `--tile-cache` (SNAP memory cache, e.g. `2048M`, `4096M`)
- `--threads` (GPT worker threads)

Starting points:
- Laptop: `--tile-cache 2048M --threads 4`
- Workstation: `--tile-cache 4096M --threads 8`

If you hit memory pressure or instability, reduce cache and/or threads.


## Logs

- Backscatter log: `backscatter_processing.log`
- Coherence log: `coherence_processing.log`


## Troubleshooting

- Error: `SNAP GPT not found in PATH`
	- Ensure SNAP is installed and `gpt` is discoverable from terminal.

- No products found in download step
	- Widen date range.
	- Verify ROI location and CRS.
	- Verify Copernicus credentials/API access.

- No valid coherence pairs found
	- Increase date range.
	- Confirm products are same track/orbit direction and within baseline constraints.

- Field extraction generates few/no files
	- Check that field masks exist from step 4.
	- Check raster coverage against field bounds.
	- Ensure CRS consistency in source rasters and masks.

