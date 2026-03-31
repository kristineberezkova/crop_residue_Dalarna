import argparse
import os
import shutil
import subprocess
import logging
from pathlib import Path
from datetime import datetime

# =============================================================================
# SNAP / PROCESSING CONSTANTS
# =============================================================================
DEM_NAME       = "Copernicus 30m Global DEM"
MAP_PROJECTION = "EPSG:32633"   # UTM 33N (Sweden)

# =============================================================================
# CLI ARGUMENT PARSING
# =============================================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description="Process Sentinel-1 SLC backscatter (Gamma0 dB) using SNAP GPT.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--input-dir", required=True,
        help="Directory containing S1 .zip or .SAFE files"
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="Root output directory (sub-folders created automatically)"
    )
    parser.add_argument(
        "--subswath", default="IW3", choices=["IW1", "IW2", "IW3"],
        help="IW subswath to process"
    )
    parser.add_argument(
        "--roi-shapefile", default=None,
        help="Optional shapefile path to subset the final output to an ROI"
    )
    parser.add_argument(
        "--tile-cache", default="2048M",
        help="SNAP GPT tile cache size (e.g. 2048M, 4096M)"
    )
    parser.add_argument(
        "--threads", type=int, default=4,
        help="Number of GPT worker threads"
    )
    return parser.parse_args()


# =============================================================================
# LOGGING  (configured after args are parsed so the log name can vary)
# =============================================================================
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("backscatter_processing.log", encoding="utf-8"),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


# =============================================================================
# BACKSCATTER PROCESSOR
# =============================================================================
class BackscatterProcessor:

    def __init__(self, input_dir, output_dir, subswath, roi_shapefile, tile_cache, threads, logger):
        """
        Process backscatter for SLC products.

        Args:
            input_dir:      Directory containing S1 .zip or .SAFE files
            output_dir:     Output directory for processed backscatter
            subswath:       IW subswath to process (IW1 / IW2 / IW3)
            roi_shapefile:  Optional shapefile to subset final output
            tile_cache:     SNAP GPT tile cache string (e.g. '2048M')
            threads:        Number of GPT worker threads
            logger:         Python logger instance
        """
        self.input_dir     = Path(input_dir)
        self.output_dir    = Path(output_dir)
        self.subswath      = subswath
        self.roi_shapefile = roi_shapefile
        self.tile_cache    = tile_cache
        self.threads       = threads
        self.logger        = logger

        self.backscatter_dir = self.output_dir / "backscatter"
        self.temp_dir        = self.output_dir / "temp_backscatter"
        self.graphs_dir      = self.output_dir / "graphs"

        for d in [self.backscatter_dir, self.temp_dir, self.graphs_dir]:
            d.mkdir(parents=True, exist_ok=True)

        self._validate_environment()

    # -------------------------------------------------------------------------
    def _validate_environment(self):
        self.logger.info("Validating environment...")

        if not shutil.which("gpt"):
            raise RuntimeError("SNAP GPT not found in PATH")

        if not self.input_dir.exists():
            raise FileNotFoundError(f"Input directory not found: {self.input_dir}")

        self.logger.info("Environment OK")

    # -------------------------------------------------------------------------
    def find_products(self):
        """Find all Sentinel-1 products in input directory."""
        products  = list(self.input_dir.glob("S1*.zip"))
        products += [p for p in self.input_dir.glob("S1*.SAFE") if p.is_dir()]
        products.sort()
        self.logger.info(f"Found {len(products)} Sentinel-1 products")
        return products

    # -------------------------------------------------------------------------
    def backscatter_graph_slc(self, input_file, output_file):
        """
        Generate SNAP graph for SLC backscatter processing.

        Processing chain:
          Read → Apply-Orbit → ThermalNoiseRemoval → Calibration (Gamma0)
          → TOPSAR-Split → TOPSAR-Deburst → Speckle-Filter
          → Terrain-Correction → LinearTodB → [Subset] → Write
        """
        if self.roi_shapefile:
            final_source = "Subset"
            subset_node  = f"""
  <node id="Subset">
    <operator>Subset</operator>
    <sources>
      <sourceProduct refid="LinearTodB"/>
    </sources>
    <parameters>
      <geoRegion>{self.roi_shapefile}</geoRegion>
      <copyMetadata>true</copyMetadata>
    </parameters>
  </node>"""
        else:
            final_source = "LinearTodB"
            subset_node  = ""

        return f"""<graph id="Backscatter_SLC">
  <version>1.0</version>

  <node id="Read">
    <operator>Read</operator>
    <parameters>
      <file>{input_file}</file>
    </parameters>
  </node>

  <node id="Orbit">
    <operator>Apply-Orbit-File</operator>
    <sources>
      <sourceProduct refid="Read"/>
    </sources>
    <parameters>
      <orbitType>Sentinel Precise (Auto Download)</orbitType>
      <continueOnFail>false</continueOnFail>
    </parameters>
  </node>

  <node id="ThermalNoise">
    <operator>ThermalNoiseRemoval</operator>
    <sources>
      <sourceProduct refid="Orbit"/>
    </sources>
    <parameters>
      <removeThermalNoise>true</removeThermalNoise>
      <reIntroduceThermalNoise>false</reIntroduceThermalNoise>
    </parameters>
  </node>

  <node id="Calibration">
    <operator>Calibration</operator>
    <sources>
      <sourceProduct refid="ThermalNoise"/>
    </sources>
    <parameters>
      <outputSigmaBand>false</outputSigmaBand>
      <outputGammaBand>true</outputGammaBand>
      <outputBetaBand>false</outputBetaBand>
      <selectedPolarisations>VV,VH</selectedPolarisations>
      <outputImageScaleInDb>false</outputImageScaleInDb>
    </parameters>
  </node>

  <node id="Split">
    <operator>TOPSAR-Split</operator>
    <sources>
      <sourceProduct refid="Calibration"/>
    </sources>
    <parameters>
      <subswath>{self.subswath}</subswath>
      <selectedPolarisations>VV,VH</selectedPolarisations>
    </parameters>
  </node>

  <node id="Deburst">
    <operator>TOPSAR-Deburst</operator>
    <sources>
      <sourceProduct refid="Split"/>
    </sources>
    <parameters>
      <selectedPolarisations>VV,VH</selectedPolarisations>
    </parameters>
  </node>

  <node id="Speckle">
    <operator>Speckle-Filter</operator>
    <sources>
      <sourceProduct refid="Deburst"/>
    </sources>
    <parameters>
      <filter>Lee Sigma</filter>
      <filterSizeX>5</filterSizeX>
      <filterSizeY>5</filterSizeY>
      <dampingFactor>2</dampingFactor>
      <estimateENL>true</estimateENL>
      <enl>1.0</enl>
      <numLooksStr>1</numLooksStr>
      <targetWindowSizeStr>3x3</targetWindowSizeStr>
      <sigmaStr>0.9</sigmaStr>
      <anSize>50</anSize>
    </parameters>
  </node>

  <node id="TC">
    <operator>Terrain-Correction</operator>
    <sources>
      <sourceProduct refid="Speckle"/>
    </sources>
    <parameters>
      <demName>{DEM_NAME}</demName>
      <demResamplingMethod>BILINEAR_INTERPOLATION</demResamplingMethod>
      <externalDEMNoDataValue>0.0</externalDEMNoDataValue>
      <imgResamplingMethod>BILINEAR_INTERPOLATION</imgResamplingMethod>
      <mapProjection>{MAP_PROJECTION}</mapProjection>
      <pixelSpacingInMeter>10.0</pixelSpacingInMeter>
      <nodataValueAtSea>false</nodataValueAtSea>
      <saveDEM>false</saveDEM>
      <saveLatLon>false</saveLatLon>
      <saveIncidenceAngleFromEllipsoid>false</saveIncidenceAngleFromEllipsoid>
      <saveLocalIncidenceAngle>false</saveLocalIncidenceAngle>
      <saveProjectedLocalIncidenceAngle>false</saveProjectedLocalIncidenceAngle>
      <saveSelectedSourceBand>true</saveSelectedSourceBand>
      <applyRadiometricNormalization>true</applyRadiometricNormalization>
      <saveSigmaNought>false</saveSigmaNought>
      <saveGammaNought>false</saveGammaNought>
      <saveBetaNought>false</saveBetaNought>
    </parameters>
  </node>

  <node id="LinearTodB">
    <operator>LinearToFromdB</operator>
    <sources>
      <sourceProduct refid="TC"/>
    </sources>
  </node>
{subset_node}

  <node id="Write">
    <operator>Write</operator>
    <sources>
      <sourceProduct refid="{final_source}"/>
    </sources>
    <parameters>
      <file>{output_file}</file>
      <formatName>GeoTIFF-BigTIFF</formatName>
    </parameters>
  </node>

</graph>"""

    # -------------------------------------------------------------------------
    def run_graph(self, graph_xml, name):
        """Execute SNAP GPT graph with the configured memory and thread settings."""
        graph_file = self.graphs_dir / f"{name}.xml"
        graph_file.write_text(graph_xml, encoding="utf-8")

        cmd = [
            "gpt",
            str(graph_file),
            "-c", self.tile_cache,
            "-q", str(self.threads)
        ]

        self.logger.info(f"Running {name} (tile cache: {self.tile_cache}, threads: {self.threads})")

        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            self.logger.info(f"Successfully completed {name}")
            return True

        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed processing {name}")
            self.logger.error(f"Error output: {e.stderr}")
            return False

    # -------------------------------------------------------------------------
    def process_backscatter(self, product):
        """Process a single product for backscatter."""
        timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_name = f"Gamma0_dB_{product.stem}_{timestamp}.tif"
        output_file = self.backscatter_dir / output_name

        self.logger.info(f"Processing backscatter: {product.name}")
        self.logger.info(f"  Product type: SLC")
        self.logger.info(f"  Subswath:     {self.subswath}")
        self.logger.info(f"  Output format: Gamma0 in dB")

        graph_xml = self.backscatter_graph_slc(product, output_file)
        success   = self.run_graph(graph_xml, f"backscatter_{product.stem}_{timestamp}")

        if success and output_file.exists():
            self.logger.info(f"Output saved: {output_file}")
            return output_file
        else:
            self.logger.error(f"Failed to create output: {output_file}")
            return None

    # -------------------------------------------------------------------------
    def process_all(self):
        """Process all products in input directory."""
        products = self.find_products()

        if not products:
            self.logger.warning("No products found to process")
            return []

        results = []
        for i, product in enumerate(products, 1):
            self.logger.info(f"Processing {i}/{len(products)}: {product.name}")
            output = self.process_backscatter(product)
            results.append(output)

        successful = [r for r in results if r is not None]
        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"BACKSCATTER PROCESSING COMPLETE")
        self.logger.info(f"{'='*60}")
        self.logger.info(f"Total products: {len(products)}")
        self.logger.info(f"Successful:     {len(successful)}")
        self.logger.info(f"Failed:         {len(products) - len(successful)}")
        self.logger.info(f"Output dir:     {self.backscatter_dir}")
        self.logger.info(f"Output format:  Gamma0 in dB (decibels)")
        self.logger.info(f"{'='*60}\n")

        return successful

    # -------------------------------------------------------------------------
    def cleanup_temp(self):
        """Remove temporary files."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
            self.logger.info("Temporary files cleaned up")


# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    args   = parse_args()
    logger = setup_logging()

    processor = BackscatterProcessor(
        input_dir     = args.input_dir,
        output_dir    = args.output_dir,
        subswath      = args.subswath,
        roi_shapefile = args.roi_shapefile,
        tile_cache    = args.tile_cache,
        threads       = args.threads,
        logger        = logger,
    )

    try:
        results = processor.process_all()
        # processor.cleanup_temp()   # uncomment to remove temp files

        logger.info("\n" + "="*60)
        logger.info("PROCESSING CHAIN FOR SLC PRODUCTS:")
        logger.info("="*60)
        logger.info("1.  Read Sentinel-1 SLC product")
        logger.info("2.  Apply orbit file")
        logger.info("3.  Thermal noise removal")
        logger.info("4.  Calibrate to Gamma0 (linear)")
        logger.info("5.  TOPSAR Split (select subswath)")
        logger.info("6.  TOPSAR Deburst")
        logger.info("7.  Speckle filtering (Lee Sigma)")
        logger.info("8.  Terrain correction with radiometric normalisation")
        logger.info("9.  Convert linear to dB")
        logger.info("10. Optional: Subset to ROI")
        logger.info("11. Write GeoTIFF")
        logger.info("="*60)
        logger.info(f"Output: Gamma0 backscatter in DECIBELS (dB)")
        logger.info(f"Expected range: -30 to +5 dB (typical)")
        logger.info("="*60 + "\n")

    except Exception as e:
        logger.error(f"Processing failed: {e}", exc_info=True)
        raise