#!/usr/bin/env python3
""": Block-coverage audit and gap-fill for data/raw/.

Scans existing parquet files to detect block gaps, then fills them via Dune.
Priority target: Nov 2025 → Feb 2026 (captures both golden episodes).

Usage:
 # Audit coverage without writing anything
 python scripts/ingest_gap_fill.py --check-coverage

 # Fill all gaps for a specific protocol
 python scripts/ingest_gap_fill.py --fill --protocol aave_v2 --events liquidations

 # Fill Oct 2025 golden episode window only
 python scripts/ingest_gap_fill.py --fill --protocol aave_v2 \\
 --start-block 23525000 --end-block 23575000

 # Check if golden episodes are present
 python scripts/ingest_gap_fill.py --check-coverage --golden-only

 # Surgical cryo extraction for a specific block range
 python scripts/ingest_gap_fill.py --cryo --protocol aave_v2 \\
 --start-block 23525000 --end-block 23527000 \\
 --contract 0x7d2768de32b0b80b7a3454c06bdac94a69ddc7a9
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cascadesignal.ingest.gap_fill import (
 audit_coverage,
 fill_gaps,
 fill_with_cryo,
 print_coverage_report,
 GOLDEN_EPISODES_GAPS,
)

logging.basicConfig(
 level=logging.INFO,
 format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
 datefmt="%H:%M:%S",
)
log = logging.getLogger("ingest_gap_fill")

# SQL name per (protocol, event_category) — matches ingest_dune.py QUERIES
_SQL_NAMES = {
 ("aave_v2", "liquidations"): "aave_v2_liquidations",
 ("aave_v2", "core"): "aave_v2_core_events",
 ("aave_v3", "liquidations"): "aave_v3_liquidations",
 ("aave_v3", "core"): "aave_v3_core_events",
 ("compound_v2", "liquidations"): "compound_v2_liquidations",
 ("compound_v3", "liquidations"): "compound_v3_absorb",
 ("maker", "liquidations"): "maker_liquidations",
}


def main -> None:
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument(
 "--check-coverage", action="store_true", help="Print coverage report and exit"
 )
 parser.add_argument(
 "--golden-only", action="store_true", help="Only check golden episode coverage"
 )
 parser.add_argument(
 "--fill", action="store_true", help="Fill detected gaps via Dune"
 )
 parser.add_argument(
 "--cryo",
 action="store_true",
 help="Fill a specific range via cryo (requires --contract)",
 )
 parser.add_argument("--protocol", help="Protocol to gap-fill (e.g. aave_v2)")
 parser.add_argument(
 "--events",
 default="liquidations",
 help="Event category (default: liquidations)",
 )
 parser.add_argument(
 "--start-block", type=int, help="Restrict fill to this start block"
 )
 parser.add_argument("--end-block", type=int, help="Restrict fill to this end block")
 parser.add_argument("--contract", help="Contract address for cryo extraction")
 parser.add_argument(
 "--chain-id", type=int, default=1, help="EVM chain ID (default: 1)"
 )
 parser.add_argument("--data-dir", default="data/raw", help="Data lake root")
 parser.add_argument("--api-key", default=None, help="Dune API key")
 args = parser.parse_args

 data_dir = Path(args.data_dir)

 if args.check_coverage:
 if args.golden_only:
 cov = audit_coverage(data_dir)
 print("\nGolden episode coverage check:")
 from cascadesignal.ingest.gap_fill import _check_episode_covered

 all_ok = True
 for name, ep_start, ep_end in GOLDEN_EPISODES_GAPS:
 covered = _check_episode_covered(cov, ep_start, ep_end)
 mark = "✓" if covered else "✗ MISSING"
 print(f" {name}: blocks {ep_start:,}–{ep_end:,} → {mark}")
 if not covered:
 all_ok = False
 sys.exit(0 if all_ok else 1)
 else:
 print_coverage_report(data_dir, golden_check=True)
 return

 if args.cryo:
 if not args.protocol or not args.contract:
 log.error("--cryo requires --protocol and --contract")
 sys.exit(1)
 if not args.start_block or not args.end_block:
 log.error("--cryo requires --start-block and --end-block")
 sys.exit(1)
 rpc = os.environ.get("ARCHIVE_RPC_URL")
 fill_with_cryo(
 protocol=args.protocol,
 chain_id=args.chain_id,
 start_block=args.start_block,
 end_block=args.end_block,
 contract_address=args.contract,
 rpc_url=rpc,
 data_dir=data_dir,
 )
 return

 if args.fill:
 if not args.protocol:
 log.error("--fill requires --protocol")
 sys.exit(1)
 sql_key = (args.protocol, args.events)
 sql_name = _SQL_NAMES.get(sql_key)
 if not sql_name:
 log.error("Unknown (protocol, events) combo: %s", sql_key)
 log.error("Valid combos: %s", list(_SQL_NAMES.keys))
 sys.exit(1)

 api_key = args.api_key or os.environ.get("DUNE_API_KEY")
 if not api_key:
 log.error("DUNE_API_KEY required for --fill")
 sys.exit(1)

 if args.start_block and args.end_block:
 # Direct fill for a specified range rather than detected gaps
 from cascadesignal.ingest.dune import DuneIngester

 ingester = DuneIngester(api_key=api_key, data_dir=data_dir)
 rows = ingester.ingest(
 protocol=args.protocol,
 sql_name=sql_name,
 start_block=args.start_block,
 end_block=args.end_block,
 chain_id=args.chain_id,
 )
 log.info("Wrote %d rows", rows)
 else:
 fill_gaps(
 protocol=args.protocol,
 chain_id=args.chain_id,
 sql_name=sql_name,
 data_dir=data_dir,
 dune_api_key=api_key,
 )
 return

 parser.print_help


if __name__ == "__main__":
 main
