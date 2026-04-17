import argparse
import geopandas as gpd
import pandas as pd
import os
from pathlib import Path
from shapely.geometry import box
from phidown.search import CopernicusDataSearcher
from phidown import download_by_name

# =============================================================================
# CLI ARGUMENT PARSING
# =============================================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description="Search and download Sentinel-1 SLC scenes for coherence processing.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--roi", required=True,
        help="Path to the ROI shapefile (e.g. /data/an_riks.shp)"
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="Directory where downloaded scenes will be saved"
    )
    parser.add_argument(
        "--start-date", default="2024-06-01T00:00:00",
        help="Search start date in ISO format (YYYY-MM-DDTHH:MM:SS)"
    )
    parser.add_argument(
        "--end-date", default="2024-09-30T23:59:59",
        help="Search end date in ISO format (YYYY-MM-DDTHH:MM:SS)"
    )
    parser.add_argument(
        "--max-baseline-days", type=int, default=12,
        help="Maximum temporal baseline (days) for valid coherence pairs"
    )
    parser.add_argument(
        "--max-scenes", type=int, default=12,
        help="Maximum number of scenes to download from selected track"
    )
    return parser.parse_args()


# =============================================================================
# MAIN
# =============================================================================
def main():
    args = parse_args()

    ROI_PATH      = args.roi
    DOWNLOAD_PATH = args.output_dir
    DOWNLOAD_PATH_POSIX = Path(DOWNLOAD_PATH).resolve().as_posix()

    # 1. Load and prepare the ROI
    if str(ROI_PATH).lower().endswith('.parquet'):
        gdf = gpd.read_parquet(ROI_PATH)
    else:
        gdf = gpd.read_file(ROI_PATH)
    gdf = gdf.to_crs(epsg=4326)

    my_roi_geom = gdf.geometry.union_all().simplify(0.05)
    
    # Force 2D by extracting only X,Y coordinates
    from shapely.geometry import Polygon, MultiPolygon
    def to_2d(geom):
        if geom.geom_type == 'Polygon':
            coords = [(x, y) for x, y, *_ in geom.exterior.coords]
            return Polygon(coords)
        elif geom.geom_type == 'MultiPolygon':
            return MultiPolygon([to_2d(g) for g in geom.geoms])
        else:
            return geom
    
    my_roi_geom = to_2d(my_roi_geom)
    
    # Ensure we have a single Polygon (not MultiPolygon), otherwise use bounding box
    if my_roi_geom.geom_type != 'Polygon':
        print(f"Geometry is {my_roi_geom.geom_type}, converting to bounding box")
        my_roi_geom = box(*my_roi_geom.bounds)
    
    # Check validity
    if not my_roi_geom.is_valid:
        print("Polygon is invalid, using bounding box instead")
        my_roi_geom = box(*my_roi_geom.bounds)
    
    my_roi_wkt  = my_roi_geom.wkt

    if len(my_roi_wkt) > 5000:
        print(f"WKT too long ({len(my_roi_wkt)} chars), using bounding box")
        bounds     = my_roi_geom.bounds
        my_roi_wkt = box(*bounds).wkt

    print(f"AOI WKT length: {len(my_roi_wkt)} chars")
    print(f"AOI WKT: {my_roi_wkt}")

    # 2. Search
    searcher = CopernicusDataSearcher()
    searcher.query_by_filter(
        collection_name='SENTINEL-1',
        product_type='SLC',
        aoi_wkt=my_roi_wkt,
        start_date=args.start_date,
        end_date=args.end_date
    )

    print("Executing query...")
    df = searcher.execute_query()
    print(f"Found {len(df)} scenes")

    if df.empty:
        print("No products found.")
        exit()

    print("Columns:", df.columns.tolist())

    # 3. Parse product name metadata
    # Example: S1A_IW_SLC__1SDV_20240901T162956_20240901T163023_055474_06C47A_E16F.SAFE
    # After strip + split on '_':
    # ['S1A', 'IW', 'SLC', '', '1SDV', '20240901T162956', '20240901T163023', '055474', '06C47A', 'E16F']
    #   [0]   [1]   [2]  [3]   [4]          [5]                  [6]            [7]       [8]      [9]
    def parse_product_name(name):
        try:
            name_clean = name.replace('.SAFE', '')
            parts      = name_clean.split('_')

            platform  = parts[0]        # S1A
            date      = parts[5][:8]    # YYYYMMDD from start timestamp
            abs_orbit = int(parts[7])   # absolute orbit number (decimal)

            if platform == 'S1A':
                rel_orbit = (abs_orbit - 73) % 175 + 1
            else:
                rel_orbit = None  # skip non-S1A

            return {
                'platformName': platform,
                'startDate':    date,
                'absoluteOrbit': abs_orbit,
                'relativeOrbit': rel_orbit,
            }
        except Exception as e:
            print(f"Failed to parse: {name} — {e}")
            return None

    metadata = df['Name'].apply(parse_product_name)
    df['platformName']   = metadata.apply(lambda x: x['platformName']   if x else None)
    df['startDate']      = metadata.apply(lambda x: x['startDate']      if x else None)
    df['absoluteOrbit']  = metadata.apply(lambda x: x['absoluteOrbit']  if x else None)
    df['relativeOrbit']  = metadata.apply(lambda x: x['relativeOrbit']  if x else None)

    # 4. Filter to S1A only
    df = df[df['platformName'] == 'S1A'].copy()
    print(f"Filtered to S1A only: {len(df)} scenes remaining")

    if df.empty:
        print("No S1A scenes found.")
        exit()

    # 5. Orbit direction
    direction_col = None
    for candidate in ['orbitDirection', 'sat:orbit_state', 'OrbitDirection', 'orbitdirection']:
        if candidate in df.columns:
            direction_col = candidate
            break

    if direction_col:
        df['orbitDirection'] = df[direction_col].str.upper()
        print(f"Orbit direction sourced from column: '{direction_col}'")
    else:
        print("WARNING: No orbit direction column found in API response.")
        print("         Grouping by relative orbit only — verify ASC/DESC manually.")
        df['orbitDirection'] = 'UNKNOWN'

    # 6. Summary
    print(f"\nPlatforms: {df['platformName'].unique()}")
    print(f"Date range: {df['startDate'].min()} to {df['startDate'].max()}")
    print(f"Relative orbits: {sorted(df['relativeOrbit'].dropna().unique())}")
    print(f"Orbit directions: {df['orbitDirection'].unique()}")
    print(f"\nAll scenes:")
    for _, row in df.sort_values('startDate').iterrows():
        print(f"  {row['startDate']} | orbit {row['relativeOrbit']} | {row['orbitDirection']} | {row['Name']}")

    # 7. Find valid coherence pairs and keep all scenes in best track
    grouped = df.groupby(['relativeOrbit', 'orbitDirection'])

    best_group_key = None
    best_group_rows = None
    best_pair_count = -1
    best_mean_baseline = float('inf')

    print(f"\nSearching for coherence pairs...")
    for group_key, group_df in grouped:
        rel_orbit, direction = group_key
        unique_dates = sorted(group_df['startDate'].unique())

        if len(unique_dates) < 2:
            continue

        valid_pairs = []

        for i in range(len(unique_dates) - 1):
            d1            = pd.to_datetime(unique_dates[i])
            d2            = pd.to_datetime(unique_dates[i + 1])
            baseline_days = (d2 - d1).days

            print(f"  Candidate: orbit {rel_orbit} {direction} | {unique_dates[i]} & {unique_dates[i+1]} | {baseline_days}d baseline")

            if baseline_days <= args.max_baseline_days:
                valid_pairs.append((unique_dates[i], unique_dates[i + 1], baseline_days))

        if not valid_pairs:
            continue

        mean_baseline = sum(v[2] for v in valid_pairs) / len(valid_pairs)
        if (len(valid_pairs) > best_pair_count) or (
            len(valid_pairs) == best_pair_count and mean_baseline < best_mean_baseline
        ):
            pair_dates = sorted({d for d1, d2, _ in valid_pairs for d in (d1, d2)})
            best_group_rows = (
                group_df[group_df['startDate'].isin(pair_dates)]
                .sort_values('startDate')
                .drop_duplicates(subset=['startDate'])
                .copy()
            )
            best_group_key = group_key
            best_pair_count = len(valid_pairs)
            best_mean_baseline = mean_baseline

    if best_group_rows is None or best_group_rows.empty:
        print("\nNo valid coherence pairs found. Try widening the date range.")
        exit()

    if len(best_group_rows) > args.max_scenes:
        best_group_rows = best_group_rows.head(args.max_scenes)

    rel_orbit, direction = best_group_key

    print(f"\n--- Selected Coherence Track ---")
    print(f"Relative orbit: {rel_orbit}")
    print(f"Orbit direction: {direction}")
    print(f"Valid pairs in track: {best_pair_count}")
    print(f"Mean baseline: {best_mean_baseline:.1f} days")
    print(f"Scenes selected for download: {len(best_group_rows)}")
    for _, row in best_group_rows.iterrows():
        print(f"  {row['startDate']} | orbit {row['relativeOrbit']} | {row['orbitDirection']}")
        print(f"    {row['Name']}")
        print(f"    Size: {row['ContentLength'] / 1e9:.2f} GB")

    # 8. Download
    os.makedirs(DOWNLOAD_PATH, exist_ok=True)

    print(f"\nDownloading {len(best_group_rows)} products...")
    print(f"Target folder: {DOWNLOAD_PATH_POSIX}")
    for _, row in best_group_rows.iterrows():
        product_name = row['Name']
        print(f"Downloading: {product_name}...")
        success = download_by_name(
            product_name=product_name,
            output_dir=DOWNLOAD_PATH_POSIX,
            show_progress=True
        )
        print(f"  {'✓ Done' if success else '✗ Failed'}")


if __name__ == "__main__":
    main()