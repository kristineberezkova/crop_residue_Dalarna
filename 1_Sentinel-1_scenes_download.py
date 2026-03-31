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
        "--start-date", default="2024-09-18T00:00:00",
        help="Search start date in ISO format (YYYY-MM-DDTHH:MM:SS)"
    )
    parser.add_argument(
        "--end-date", default="2024-09-30T23:59:59",
        help="Search end date in ISO format (YYYY-MM-DDTHH:MM:SS)"
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
    gdf = gpd.read_file(ROI_PATH)
    gdf = gdf.to_crs(epsg=4326)

    my_roi_geom = gdf.geometry.union_all().simplify(0.05)
    my_roi_wkt  = my_roi_geom.wkt

    if len(my_roi_wkt) > 5000:
        print(f"WKT too long ({len(my_roi_wkt)} chars), falling back to bounding box")
        bounds     = my_roi_geom.bounds
        my_roi_wkt = box(*bounds).wkt
        print(f"Bounding box: {my_roi_wkt}")
    else:
        print(f"Using simplified polygon ({len(my_roi_wkt)} chars)")

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

    # 7. Find valid coherence pairs
    grouped = df.groupby(['relativeOrbit', 'orbitDirection'])

    best_pair     = None
    best_baseline = float('inf')

    print(f"\nSearching for coherence pairs...")
    for group_key, group_df in grouped:
        rel_orbit, direction = group_key
        unique_dates = sorted(group_df['startDate'].unique())

        if len(unique_dates) < 2:
            continue

        for i in range(len(unique_dates) - 1):
            d1            = pd.to_datetime(unique_dates[i])
            d2            = pd.to_datetime(unique_dates[i + 1])
            baseline_days = (d2 - d1).days

            print(f"  Candidate: orbit {rel_orbit} {direction} | {unique_dates[i]} & {unique_dates[i+1]} | {baseline_days}d baseline")

            if baseline_days < best_baseline:
                best_baseline = baseline_days
                scene1    = group_df[group_df['startDate'] == unique_dates[i]].iloc[[0]]
                scene2    = group_df[group_df['startDate'] == unique_dates[i+1]].iloc[[0]]
                best_pair = pd.concat([scene1, scene2])

    if best_pair is None:
        print("\nNo valid coherence pairs found. Try widening the date range.")
        exit()

    print(f"\n--- Selected Coherence Pair ---")
    print(f"Temporal baseline: {best_baseline} days")
    for _, row in best_pair.iterrows():
        print(f"  {row['startDate']} | orbit {row['relativeOrbit']} | {row['orbitDirection']}")
        print(f"    {row['Name']}")
        print(f"    Size: {row['ContentLength'] / 1e9:.2f} GB")

    # 8. Download
    os.makedirs(DOWNLOAD_PATH, exist_ok=True)

    print(f"\nDownloading {len(best_pair)} products...")
    print(f"Target folder: {DOWNLOAD_PATH_POSIX}")
    for _, row in best_pair.iterrows():
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