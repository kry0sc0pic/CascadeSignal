#!/usr/bin/env python3
""": Bootstrap Aave v3 arXiv dataset (2512.11363).

Tries to download from Zenodo DOI 10.5281/zenodo.17898640.
If the record is not yet published, prints instructions for the Dune fallback.

Usage:
 python scripts/ingest_arxiv.py
 python scripts/ingest_arxiv.py --dry-run
 python scripts/ingest_arxiv.py --chain-id 1 # Ethereum only
 python scripts/ingest_arxiv.py --status # check Zenodo availability
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Ensure project package is importable when run from repo root
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cascadesignal.ingest.arxiv_aave_v3 import ArxivAaveV3Ingestor, check_arxiv_status

logging.basicConfig(
 level=logging.INFO,
 format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
 datefmt="%H:%M:%S",
)
log = logging.getLogger("ingest_arxiv")


def main -> None:
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument(
 "--dry-run",
 action="store_true",
 help="Print what would happen, don't write files",
 )
 parser.add_argument(
 "--status", action="store_true", help="Check Zenodo DOI availability and exit"
 )
 parser.add_argument(
 "--chain-id",
 type=int,
 default=None,
 help="Restrict to a single chain ID (e.g. 1 for Ethereum)",
 )
 parser.add_argument(
 "--data-dir", default="data/raw", help="Data lake root (default: data/raw)"
 )
 args = parser.parse_args

 if args.status:
 status = check_arxiv_status
 print(f"DOI: {status['doi']}")
 print(f"Available: {status['available']}")
 print(f"URL: {status['url'] or 'not found'}")
 if not status["available"]:
 print
 print("The Zenodo record is not yet published.")
 print("Fallback: run the Dune ingestion for Aave v3 Ethereum mainnet:")
 print(
 " python scripts/ingest_dune.py --protocol aave_v3 --events liquidations"
 )
 print(" python scripts/ingest_dune.py --protocol aave_v3 --events core")
 return

 target_chains = [args.chain_id] if args.chain_id else None
 ingestor = ArxivAaveV3Ingestor(
 data_dir=Path(args.data_dir),
 target_chains=target_chains,
 )

 log.info("Starting: arXiv Aave v3 dataset bootstrap")
 result = ingestor.run(dry_run=args.dry_run)

 if result.get("status") == "zenodo_unavailable":
 log.warning("Zenodo record not yet published.")
 log.warning("Use Dune fallback:")
 log.warning(
 " python scripts/ingest_dune.py --protocol aave_v3 --events liquidations"
 )
 log.warning(" python scripts/ingest_dune.py --protocol aave_v3 --events core")
 sys.exit(1)

 if result.get("status") == "ok":
 total = sum(result["rows"].values)
 log.info("Done. Total rows ingested: %d", total)
 for chain_id, n in sorted(result["rows"].items):
 log.info(" chain=%d: %d rows", chain_id, n)


if __name__ == "__main__":
 main
