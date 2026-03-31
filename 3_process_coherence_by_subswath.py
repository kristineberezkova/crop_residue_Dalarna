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
        description="Process Sentinel-1 SLC interferometric coherence using SNAP GPT.",
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
        "--max-baseline", type=int, default=12,
        help="Maximum temporal baseline in days for a valid coherence pair"
    )
    parser.add_argument(
        "--track-tolerance", type=int, default=60,
        help="Time-of-day tolerance in seconds for same-track check"
    )
    parser.add_argument(
        "--tile-cache", default="4096M",
        help="SNAP GPT tile cache size (e.g. 2048M, 4096M)"
    )
    parser.add_argument(
        "--threads", type=int, default=4,
        help="Number of GPT worker threads"
    )
    return parser.parse_args()


# =============================================================================
# LOGGING
# =============================================================================
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("coherence_processing.log", encoding="utf-8"),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


# =============================================================================
# COHERENCE PROCESSOR
# =============================================================================
class CoherenceProcessor:

    def __init__(self, input_dir, output_dir, subswath, max_baseline,
                 track_tolerance, tile_cache, threads, logger):
        """
        Process interferometric coherence from Sentinel-1 SLC pairs.

        Args:
            input_dir:       Directory containing S1 .zip or .SAFE files
            output_dir:      Output directory for processed coherence
            subswath:        IW subswath to process (IW1 / IW2 / IW3)
            max_baseline:    Maximum temporal baseline in days
            track_tolerance: Time-of-day tolerance in seconds for track check
            tile_cache:      SNAP GPT tile cache string
            threads:         Number of GPT worker threads
            logger:          Python logger instance
        """
        self.input_dir       = Path(input_dir)
        self.output_dir      = Path(output_dir)
        self.subswath        = subswath
        self.max_baseline    = max_baseline
        self.track_tolerance = track_tolerance
        self.tile_cache      = tile_cache
        self.threads         = threads
        self.logger          = logger

        self.coherence_dir = self.output_dir / "coherence"
        self.temp_dir      = self.output_dir / "temp_coherence"
        self.graphs_dir    = self.output_dir / "graphs"

        for d in [self.coherence_dir, self.temp_dir, self.graphs_dir]:
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
        for p in products:
            self.logger.info(f"  {p.name}")
        return products

    # -------------------------------------------------------------------------
    def parse_product_date(self, product_path):
        """
        Extract acquisition date from product name.

        S1A_IW_SLC__1SDV_20240908T050700_20240908T050727_055569_06C843_F3CB.SAFE
        parts[5] = start datetime → YYYYMMDD
        """
        try:
            name     = product_path.stem.replace('.SAFE', '')
            date_str = name.split('_')[5][:8]
            return datetime.strptime(date_str, "%Y%m%d")
        except Exception:
            self.logger.warning(f"Could not parse date from: {product_path.name}")
            return None

    # -------------------------------------------------------------------------
    def _same_track(self, product_a, product_b):
        """
        Check if two products are from the same orbit track by comparing
        time-of-day of acquisition within self.track_tolerance seconds.
        """
        try:
            name_a = product_a.stem.replace('.SAFE', '')
            name_b = product_b.stem.replace('.SAFE', '')

            time_a = datetime.strptime(name_a.split('_')[5], "%Y%m%dT%H%M%S")
            time_b = datetime.strptime(name_b.split('_')[5], "%Y%m%dT%H%M%S")

            tod_a = time_a.hour * 3600 + time_a.minute * 60 + time_a.second
            tod_b = time_b.hour * 3600 + time_b.minute * 60 + time_b.second

            diff = abs(tod_a - tod_b)
            self.logger.info(
                f"Time-of-day difference: {diff}s (tolerance: {self.track_tolerance}s)"
            )
            return diff <= self.track_tolerance

        except Exception as e:
            self.logger.warning(f"Track check failed: {e}")
            return False

    # -------------------------------------------------------------------------
    def filter_product_pairs(self, products):
        """
        Create valid product pairs based on same track and temporal baseline.

        Returns:
            List of (master, slave) tuples
        """
        pairs = []

        for i in range(len(products) - 1):
            master = products[i]
            slave  = products[i + 1]

            self.logger.info(f"Evaluating pair: {master.name} — {slave.name}")

            if not self._same_track(master, slave):
                self.logger.info("Skipping pair (different tracks)")
                continue

            master_date = self.parse_product_date(master)
            slave_date  = self.parse_product_date(slave)

            if master_date and slave_date:
                baseline_days = abs((slave_date - master_date).days)

                if baseline_days <= self.max_baseline:
                    pairs.append((master, slave))
                    self.logger.info(f"Valid pair ({baseline_days} days baseline)")
                else:
                    self.logger.info(
                        f"Skipping pair (baseline {baseline_days}d > {self.max_baseline}d)"
                    )

        self.logger.info(f"Created {len(pairs)} valid coherence pairs")
        return pairs

    # -------------------------------------------------------------------------
    def coherence_graph(self, master, slave, output_file):
        """Generate SNAP graph for coherence processing."""

        return f"""<graph id="Coherence">
  <version>1.0</version>

  <!-- READ MASTER -->
  <node id="Read-M">
    <operator>Read</operator>
    <parameters>
      <file>{master}</file>
    </parameters>
  </node>

  <!-- READ SLAVE -->
  <node id="Read-S">
    <operator>Read</operator>
    <parameters>
      <file>{slave}</file>
    </parameters>
  </node>

  <!-- APPLY ORBIT - MASTER -->
  <node id="Orbit-M">
    <operator>Apply-Orbit-File</operator>
    <sources>
      <sourceProduct refid="Read-M"/>
    </sources>
    <parameters>
      <orbitType>Sentinel Precise (Auto Download)</orbitType>
      <continueOnFail>false</continueOnFail>
    </parameters>
  </node>

  <!-- APPLY ORBIT - SLAVE -->
  <node id="Orbit-S">
    <operator>Apply-Orbit-File</operator>
    <sources>
      <sourceProduct refid="Read-S"/>
    </sources>
    <parameters>
      <orbitType>Sentinel Precise (Auto Download)</orbitType>
      <continueOnFail>false</continueOnFail>
    </parameters>
  </node>

  <!-- TOPSAR SPLIT - MASTER -->
  <node id="Split-M">
    <operator>TOPSAR-Split</operator>
    <sources>
      <sourceProduct refid="Orbit-M"/>
    </sources>
    <parameters>
      <subswath>{self.subswath}</subswath>
      <selectedPolarisations>VV</selectedPolarisations>
    </parameters>
  </node>

  <!-- TOPSAR SPLIT - SLAVE -->
  <node id="Split-S">
    <operator>TOPSAR-Split</operator>
    <sources>
      <sourceProduct refid="Orbit-S"/>
    </sources>
    <parameters>
      <subswath>{self.subswath}</subswath>
      <selectedPolarisations>VV</selectedPolarisations>
    </parameters>
  </node>

  <!-- BACK-GEOCODING -->
  <node id="BackGeo">
    <operator>Back-Geocoding</operator>
    <sources>
      <sourceProduct refid="Split-M"/>
      <sourceProduct refid="Split-S"/>
    </sources>
    <parameters>
      <demName>{DEM_NAME}</demName>
      <demResamplingMethod>BILINEAR_INTERPOLATION</demResamplingMethod>
      <externalDEMNoDataValue>0.0</externalDEMNoDataValue>
      <resamplingType>BILINEAR_INTERPOLATION</resamplingType>
      <maskOutAreaWithoutElevation>true</maskOutAreaWithoutElevation>
      <outputRangeAzimuthOffset>false</outputRangeAzimuthOffset>
      <outputDerampDemodPhase>false</outputDerampDemodPhase>
    </parameters>
  </node>

  <!-- ENHANCED SPECTRAL DIVERSITY -->
  <node id="ESD">
    <operator>Enhanced-Spectral-Diversity</operator>
    <sources>
      <sourceProduct refid="BackGeo"/>
    </sources>
    <parameters>
      <fineWinWidthStr>512</fineWinWidthStr>
      <fineWinHeightStr>512</fineWinHeightStr>
      <fineWinAccAzimuth>16</fineWinAccAzimuth>
      <fineWinAccRange>16</fineWinAccRange>
      <fineWinOversampling>128</fineWinOversampling>
      <useSuppliedRangeShift>false</useSuppliedRangeShift>
      <overallRangeShift>0.0</overallRangeShift>
      <useSuppliedAzimuthShift>false</useSuppliedAzimuthShift>
      <overallAzimuthShift>0.0</overallAzimuthShift>
    </parameters>
  </node>

  <!-- COHERENCE ESTIMATION -->
  <node id="Coherence">
    <operator>Coherence</operator>
    <sources>
      <sourceProduct refid="ESD"/>
    </sources>
    <parameters>
      <cohWinAz>4</cohWinAz>
      <cohWinRg>16</cohWinRg>
      <squarePixel>true</squarePixel>
      <subtractFlatEarthPhase>false</subtractFlatEarthPhase>
      <srpPolynomialDegree>5</srpPolynomialDegree>
      <srpNumberPoints>501</srpNumberPoints>
      <orbitDegree>3</orbitDegree>
    </parameters>
  </node>

  <!-- TOPSAR DEBURST -->
  <node id="Deburst">
    <operator>TOPSAR-Deburst</operator>
    <sources>
      <sourceProduct refid="Coherence"/>
    </sources>
    <parameters>
      <selectedPolarisations>VV</selectedPolarisations>
    </parameters>
  </node>

  <!-- TERRAIN CORRECTION -->
  <node id="TC">
    <operator>Terrain-Correction</operator>
    <sources>
      <sourceProduct refid="Deburst"/>
    </sources>
    <parameters>
      <demName>{DEM_NAME}</demName>
      <demResamplingMethod>BILINEAR_INTERPOLATION</demResamplingMethod>
      <externalDEMNoDataValue>0.0</externalDEMNoDataValue>
      <imgResamplingMethod>BILINEAR_INTERPOLATION</imgResamplingMethod>
      <mapProjection>{MAP_PROJECTION}</mapProjection>
      <pixelSpacingInMeter>10.0</pixelSpacingInMeter>
      <nodataValueAtSea>true</nodataValueAtSea>
      <saveDEM>false</saveDEM>
      <saveLatLon>false</saveLatLon>
      <saveIncidenceAngleFromEllipsoid>false</saveIncidenceAngleFromEllipsoid>
      <saveLocalIncidenceAngle>false</saveLocalIncidenceAngle>
      <saveProjectedLocalIncidenceAngle>false</saveProjectedLocalIncidenceAngle>
      <saveSelectedSourceBand>true</saveSelectedSourceBand>
    </parameters>
  </node>

  <!-- WRITE OUTPUT -->
  <node id="Write">
    <operator>Write</operator>
    <sources>
      <sourceProduct refid="TC"/>
    </sources>
    <parameters>
      <file>{output_file}</file>
      <formatName>GeoTIFF-BigTIFF</formatName>
    </parameters>
  </node>

</graph>"""

    # -------------------------------------------------------------------------
    def run_graph(self, graph_xml, name):
        """Execute SNAP GPT graph, streaming output line by line for monitoring."""
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
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            for line in process.stdout:
                line = line.rstrip()
                if line:
                    self.logger.info(f"[GPT] {line}")

            process.wait()

            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, cmd)

            self.logger.info(f"Successfully completed {name}")
            return True

        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed processing {name} (return code {e.returncode})")
            return False

    # -------------------------------------------------------------------------
    def process_coherence_pair(self, master, slave):
        """Process a single coherence pair."""
        timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_name = f"Coh_{master.stem[:25]}_{slave.stem[:25]}_{timestamp}.tif"
        output_file = self.coherence_dir / output_name

        self.logger.info(f"Processing coherence pair:")
        self.logger.info(f"  Master:   {master.name}")
        self.logger.info(f"  Slave:    {slave.name}")
        self.logger.info(f"  Subswath: {self.subswath}")
        self.logger.info(f"  Output:   {output_file}")

        graph_xml = self.coherence_graph(master, slave, output_file)
        success   = self.run_graph(graph_xml, f"coherence_{timestamp}")

        if success and output_file.exists():
            self.logger.info(f"Output saved: {output_file}")
            return output_file
        else:
            self.logger.error(f"Failed to create output: {output_file}")
            return None

    # -------------------------------------------------------------------------
    def process_all(self):
        """Process all valid coherence pairs in input directory."""
        products = self.find_products()

        if len(products) < 2:
            self.logger.warning("Need at least 2 products for coherence processing")
            return []

        pairs = self.filter_product_pairs(products)

        if not pairs:
            self.logger.warning("No valid coherence pairs found")
            return []

        results = []
        for i, (master, slave) in enumerate(pairs, 1):
            self.logger.info(f"\n{'='*60}")
            self.logger.info(f"Processing pair {i}/{len(pairs)}")
            self.logger.info(f"{'='*60}")

            output = self.process_coherence_pair(master, slave)
            results.append(output)

        successful = [r for r in results if r is not None]
        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"COHERENCE PROCESSING COMPLETE")
        self.logger.info(f"{'='*60}")
        self.logger.info(f"Total pairs:  {len(pairs)}")
        self.logger.info(f"Successful:   {len(successful)}")
        self.logger.info(f"Failed:       {len(pairs) - len(successful)}")
        self.logger.info(f"Output dir:   {self.coherence_dir}")
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

    processor = CoherenceProcessor(
        input_dir       = args.input_dir,
        output_dir      = args.output_dir,
        subswath        = args.subswath,
        max_baseline    = args.max_baseline,
        track_tolerance = args.track_tolerance,
        tile_cache      = args.tile_cache,
        threads         = args.threads,
        logger          = logger,
    )

    try:
        results = processor.process_all()
        # processor.cleanup_temp()   # uncomment to remove temp files
    except Exception as e:
        logger.error(f"Processing failed: {e}", exc_info=True)
        raise