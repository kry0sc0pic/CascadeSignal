#!/usr/bin/env python3
""": Ingest on-chain lending events via Dune Analytics API.

Each query runs once over the full study period (date-filtered in SQL),
pays the scan cost once, then paginates results locally — no repeated chunk costs.

Requires:
 DUNE_API_KEY environment variable (or --api-key)

Usage:
 python scripts/ingest_dune.py # all protocols
 python scripts/ingest_dune.py --protocol aave_v2
 python scripts/ingest_dune.py --protocol aave_v2 --events liquidations
 python scripts/ingest_dune.py --credits # check credit usage
 python scripts/ingest_dune.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cascadesignal.ingest.dune import DuneIngester

logging.basicConfig(
 level=logging.INFO,
 format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
 datefmt="%H:%M:%S",
)
log = logging.getLogger("ingest_dune")

# (protocol, event_category) → SQL filename stem
QUERIES: dict[str, dict[str, str]] = {
 "aave_v2": {"liquidations": "aave_v2_liquidations", "core": "aave_v2_core_events"},
 "aave_v3": {"liquidations": "aave_v3_liquidations", "core": "aave_v3_core_events"},
 "compound_v2": {"liquidations": "compound_v2_liquidations"},
 "compound_v3": {"liquidations": "compound_v3_absorb"},
 "maker": {"liquidations": "maker_liquidations"},
 "chainlink": {"prices": "chainlink_prices"},
}

# P0 liquidations first, then core events
DEFAULT_ORDER = [
 ("aave_v2", "liquidations"),
 ("aave_v3", "liquidations"),
 ("compound_v2", "liquidations"),
 ("maker", "liquidations"),
 ("compound_v3", "liquidations"),
 ("aave_v2", "core"),
 ("aave_v3", "core"),
]


def main -> None:
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument("--protocol", choices=list(QUERIES.keys))
 parser.add_argument("--events", help="Event category: liquidations, core, prices")
 parser.add_argument("--api-key", default=None)
 parser.add_argument("--data-dir", default="data/raw")
 parser.add_argument("--credits", action="store_true", help="Show Dune credit usage")
 parser.add_argument("--dry-run", action="store_true")
 parser.add_argument(
 "--start-block", type=int, help="Restrict ingestion to an inclusive start block"
 )
 parser.add_argument(
 "--end-block", type=int, help="Restrict ingestion to an inclusive end block"
 )
 parser.add_argument(
 "--chunk-blocks",
 type=int,
 help="Run block-ranged ingestion in chunks of this size",
 )
 args = parser.parse_args

 if args.protocol:
 if args.events:
 if args.events not in QUERIES[args.protocol]:
 log.error(
 "Unknown event category '%s' for '%s'. Available: %s",
 args.events,
 args.protocol,
 list(QUERIES[args.protocol].keys),
 )
 sys.exit(1)
 work = [(args.protocol, args.events)]
 else:
 work = [(args.protocol, ev) for ev in QUERIES[args.protocol]]
 else:
 work = DEFAULT_ORDER

 log.info("Work items: %d", len(work))
 for p, ev in work:
 log.info(" %s/%s → %s", p, ev, QUERIES[p][ev])

 if args.dry_run:
 log.info("[DRY RUN] No queries will be executed.")
 return

 api_key = args.api_key or os.environ.get("DUNE_API_KEY")
 if not api_key:
 log.error("DUNE_API_KEY not set.")
 sys.exit(1)

 ingester = DuneIngester(api_key=api_key, data_dir=Path(args.data_dir))

 if args.credits:
 usage = ingester.check_credits
 print("Credit usage:", usage)
 return

 if (args.start_block is None) ^ (args.end_block is None):
 log.error("--start-block and --end-block must be provided together")
 sys.exit(1)

 total = 0
 for protocol, events in work:
 sql_name = QUERIES[protocol][events]
 log.info("Starting %s/%s", protocol, events)
 if args.chunk_blocks:
 start_block = (
 args.start_block if args.start_block is not None else 11_565_019
 )
 end_block = args.end_block if args.end_block is not None else 24_560_000
 rows = ingester.ingest_chunked(
 protocol=protocol,
 sql_name=sql_name,
 start_block=start_block,
 end_block=end_block,
 chunk_blocks=args.chunk_blocks,
 )
 else:
 rows = ingester.ingest(
 protocol=protocol,
 sql_name=sql_name,
 start_block=args.start_block,
 end_block=args.end_block,
 )
 total += rows
 log.info(" → %d rows", rows)

 log.info("All done. Total rows: %d", total)


if __name__ == "__main__":
 main
