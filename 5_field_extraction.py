import argparse
from pathlib import Path
from tqdm import tqdm
import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling, transform_bounds

def croptofield_with_mask(sentinelfile, maskfile, outfile):
    """Reproject Sentinel raster to mask grid and apply field mask."""

    with rasterio.open(maskfile) as mask_src:
        mask = mask_src.read(1)  # 0/1 mask
        target_transform = mask_src.transform
        target_height = mask_src.height
        target_width = mask_src.width
        target_crs = mask_src.crs
        target_bounds = mask_src.bounds
        target_profile = mask_src.profile.copy()

    with rasterio.open(sentinelfile) as src:
        if src.crs and target_crs:
            src_bounds_in_target = transform_bounds(
                src.crs,
                target_crs,
                src.bounds.left,
                src.bounds.bottom,
                src.bounds.right,
                src.bounds.top,
                densify_pts=21,
            )
            src_left, src_bottom, src_right, src_top = src_bounds_in_target
        else:
            src_left, src_bottom, src_right, src_top = (
                src.bounds.left,
                src.bounds.bottom,
                src.bounds.right,
                src.bounds.top,
            )

        # Skip unless the whole field mask extent is covered by the source raster.
        if not (
            target_bounds.left >= src_left and
            target_bounds.right <= src_right and
            target_bounds.bottom >= src_bottom and
            target_bounds.top <= src_top
        ):
            return False

        # Allocate array
        resampled = np.empty(
            (src.count, target_height, target_width),
            dtype=src.dtypes[0]
        )

        # Reproject
        reproject(
            source=rasterio.band(src, list(range(1, src.count + 1))),
            destination=resampled,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=target_transform,
            dst_crs=target_crs,
            resampling=Resampling.bilinear,
            src_nodata=src.nodata,
            dst_nodata=src.nodata
        )

        # Apply mask (set pixels outside field to nodata)
        nodata = src.nodata if src.nodata is not None else -9999
        for b in range(src.count):
            band = resampled[b]
            band[mask == 0] = nodata
            resampled[b] = band

        # Update profile
        target_profile.update({
            'driver': 'GTiff',
            'height': target_height,
            'width': target_width,
            'transform': target_transform,
            'crs': target_crs,
            'count': src.count,
            'dtype': src.dtypes[0],
            'nodata': nodata,
            'compress': 'lzw'
        })

    # Skip writing if the resampled array contains only nodata (or only NaNs)
    try:
        nodata_is_nan = np.isnan(nodata)
    except Exception:
        nodata_is_nan = False

    if nodata_is_nan:
        if np.all(np.isnan(resampled)):
            return False
    else:
        if not np.any(resampled != nodata):
            return False

    # Write output (create parent folder only for valid outputs)
    outfile.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(outfile, 'w', **target_profile) as dst:
        dst.write(resampled)

    return True


def process_directory(sentineldir, label, datasetpath):
    """Process all Sentinel rasters for all fields and save into field folders."""

    sentinelfiles = sorted(sentineldir.glob("*.tif"))
    field_dirs = sorted((datasetpath / "data").glob("*"))

    for fieldpath in tqdm(field_dirs, desc=f"{label}"):
        fieldid = fieldpath.name
        maskfile = fieldpath / f"mask_{fieldid}.tif"

        if not maskfile.exists():
            print(f"Mask not found for field {fieldid}, skipping.")
            continue

        # Output subfolder inside the field folder
        outdir = fieldpath / label

        for sentinelfile in sentinelfiles:
            outfile = outdir / f"{label}_{sentinelfile.stem}_{fieldid}.tif"

            if outfile.exists():
                continue

            try:
                success = croptofield_with_mask(sentinelfile, maskfile, outfile)
                if not success:
                    continue  # no overlap

            except Exception as e:
                print(f"Error: {fieldid}, {sentinelfile.name}: {e}")
                raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--datasetpath', required=True)
    parser.add_argument('--backscatterdir', required=True)
    parser.add_argument('--coherencedir', required=True)
    args = parser.parse_args()

    datasetpath = Path(args.datasetpath)
    backscatter_dir = Path(args.backscatterdir)
    coherence_dir = Path(args.coherencedir)

    # Extract and save backscatter
    process_directory(backscatter_dir, "backscatter", datasetpath)

    # Extract and save coherence
    process_directory(coherence_dir, "coherence", datasetpath)