#!/usr/bin/env python3
""": Flashbots mempool-dumpster ingestion.

Downloads daily mempool CSV dumps, filters to txs targeting a tracked
lending-pool contract (Aave v2/v3), and persists only the filtered rows —
see src/cascadesignal/ingest/flashbots_mempool.py for why (raw daily parquet
dumps are ~4-7 GB/day; the filtered CSV variant is ~100 MB/day and collapses
to KB after filtering).

Usage:
 # Golden-episode windows only (Oct 2025 + Feb 2026, ~15 days)
 python scripts/ingest_flashbots_mempool.py --golden-only

 # Full ticket scope: Aug 2023 -> Feb 2026
 python scripts/ingest_flashbots_mempool.py --full-backfill

 # Custom range
 python scripts/ingest_flashbots_mempool.py --start-date 2025-10-08 --end-date 2025-10-13

 # Print checkpoint coverage without downloading anything
 python scripts/ingest_flashbots_mempool.py --check-coverage
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cascadesignal.ingest.flashbots_mempool import (
 COVERAGE_START,
 GOLDEN_MEMPOOL_WINDOWS,
 STUDY_END,
 load_checkpoint,
 backfill,
)

logging.basicConfig(
 level=logging.INFO,
 format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
 datefmt="%H:%M:%S",
)
log = logging.getLogger("ingest_flashbots_mempool")


def _print_coverage(data_dir: Path) -> None:
 checkpoint = load_checkpoint(data_dir)
 if not checkpoint:
 print("No mempool checkpoint yet.")
 return
 ok = sum(1 for v in checkpoint.values if v["status"] == "ok")
 missing = sum(1 for v in checkpoint.values if v["status"] == "missing")
 errors = sum(1 for v in checkpoint.values if v["status"] == "error")
 total_filtered = sum(v.get("filtered_rows", 0) for v in checkpoint.values)
 print(
 f"Days recorded: {len(checkpoint)} (ok={ok}, missing={missing}, error={errors})"
 )
 print(f"Total filtered (lending-pool) txs: {total_filtered:,}")
 print("\nGolden episode coverage:")
 for name, start, end in GOLDEN_MEMPOOL_WINDOWS:
 day = start
 days_ok = 0
 span = (end - start).days + 1
 while day <= end:
 if checkpoint.get(day.isoformat, {}).get("status") == "ok":
 days_ok += 1
 day = date.fromordinal(day.toordinal + 1)
 mark = "✓" if days_ok == span else f"partial ({days_ok}/{span})"
 print(f" {name}: {start} -> {end} -> {mark}")


def main -> None:
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument(
 "--golden-only",
 action="store_true",
 help="Backfill only the golden-episode windows",
 )
 parser.add_argument(
 "--full-backfill",
 action="store_true",
 help="Backfill the full Aug 2023 -> Feb 2026 sub-period",
 )
 parser.add_argument("--start-date", type=str, help="YYYY-MM-DD")
 parser.add_argument("--end-date", type=str, help="YYYY-MM-DD")
 parser.add_argument(
 "--check-coverage",
 action="store_true",
 help="Print checkpoint coverage and exit",
 )
 parser.add_argument("--data-dir", type=str, default="data/raw")
 args = parser.parse_args

 data_dir = Path(args.data_dir)

 if args.check_coverage:
 _print_coverage(data_dir)
 return

 if args.golden_only:
 for name, start, end in GOLDEN_MEMPOOL_WINDOWS:
 log.info("Backfilling %s (%s -> %s)", name, start, end)
 backfill(start, end, data_dir=data_dir)
 elif args.full_backfill:
 log.info("Backfilling full sub-period %s -> %s", COVERAGE_START, STUDY_END)
 backfill(COVERAGE_START, STUDY_END, data_dir=data_dir)
 elif args.start_date and args.end_date:
 start = date.fromisoformat(args.start_date)
 end = date.fromisoformat(args.end_date)
 backfill(start, end, data_dir=data_dir)
 else:
 parser.error(
 "Specify --golden-only, --full-backfill, or --start-date/--end-date"
 )

 _print_coverage(data_dir)


if __name__ == "__main__":
 main
