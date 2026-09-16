"""Block-coverage auditing and gap-fill for the CascadeSignal data lake (CAS-6).

Scans parquet files in data/raw/ to identify gaps in block coverage per
protocol/chain, then fills those gaps via Dune or cryo.

Gap-fill priority: Nov 2025 → Feb 2026 must be complete before Mock 1 to
capture the two golden episodes (Oct 2025 flash deleveraging + Feb 2026
Fed-nomination cascade).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

import pyarrow.parquet as pq

log = logging.getLogger(__name__)

_FILENAME_PATTERN = re.compile(r"blocks_(\d{9})_(\d{9})")

# Ethereum block ranges for the two golden episodes requiring full coverage.
# Kept in sync with configs/ingest.yaml's golden_episodes (see that file's
# comments for how these were derived / the 2026-07-14 miscalibration fix).
GOLDEN_EPISODES_GAPS = [
    ("Oct 2025 flash deleveraging", 23525000, 23575000),
    ("Feb 2026 Fed-nomination", 24330000, 24405000),
]


def audit_coverage(data_dir: Path = Path("data/raw")) -> dict[str, dict]:
    """Scan data/raw/ and return block coverage per (protocol, chain_id).

    Reads actual min/max block_number from each parquet file.
    Returns a dict keyed by "{protocol}/chain={chain_id}" with:
      - covered_ranges: list of (start, end) tuples from existing files
      - gaps: list of (start, end) tuples missing from [global_start, global_end]
    """

    data_dir = Path(data_dir)
    coverage: dict[str, list[tuple[int, int]]] = {}

    for parquet_file in sorted(data_dir.rglob("*.parquet")):
        # Skip checkpoint directory
        if ".checkpoints" in str(parquet_file):
            continue
        try:
            table = pq.read_table(parquet_file, columns=["block_number"])
            col = table.column("block_number")
            values = col.to_pylist()
            if not values:
                continue
            start = int(min(values))
            end = int(max(values))
        except Exception as e:
            log.debug("Could not read block range from %s: %s", parquet_file, e)
            continue

        parts = parquet_file.relative_to(data_dir).parts
        key = f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else parts[0]
        coverage.setdefault(key, []).append((start, end))

    result = {}
    for key, ranges in coverage.items():
        ranges_sorted = sorted(ranges)
        global_start = ranges_sorted[0][0]
        global_end = ranges_sorted[-1][1]
        gaps = _find_gaps(ranges_sorted, global_start, global_end)
        result[key] = {
            "covered_ranges": ranges_sorted,
            "global_start": global_start,
            "global_end": global_end,
            "gaps": gaps,
            "total_files": len(ranges_sorted),
        }

    return result


def print_coverage_report(
    data_dir: Path = Path("data/raw"),
    golden_check: bool = True,
) -> None:
    """Print a human-readable coverage report to stdout."""
    cov = audit_coverage(data_dir)
    if not cov:
        print("No parquet files found in data/raw/. Run ingest_dune.py first.")
        return

    print(
        f"\n{'Protocol/Chain':<45} {'Files':>6} {'Start':>12} {'End':>12} {'Gaps':>5}"
    )
    print("-" * 85)
    for key, info in sorted(cov.items()):
        n_gaps = len(info["gaps"])
        print(
            f"{key:<45} {info['total_files']:>6} "
            f"{info['global_start']:>12,} {info['global_end']:>12,} "
            f"{'✓' if n_gaps == 0 else f'⚠ {n_gaps}':>5}"
        )

    if golden_check:
        print("\nGolden episode coverage:")
        for name, ep_start, ep_end in GOLDEN_EPISODES_GAPS:
            covered = _check_episode_covered(cov, ep_start, ep_end)
            mark = "✓" if covered else "✗ MISSING"
            print(f"  {name}: blocks {ep_start:,}–{ep_end:,} → {mark}")


def fill_gaps(
    protocol: str,
    chain_id: int,
    sql_name: str,
    data_dir: Path = Path("data/raw"),
    dune_api_key: Optional[str] = None,
) -> None:
    """Fill all detected block gaps for a protocol/chain using Dune."""
    from cascadesignal.ingest.dune import DuneIngester

    cov = audit_coverage(data_dir)
    key = f"{protocol}/chain={chain_id}"
    info = cov.get(key)
    if info is None:
        log.info("No coverage data for %s — skipping gap fill", key)
        return

    gaps = info["gaps"]
    if not gaps:
        log.info("No gaps found for %s", key)
        return

    log.info("Filling %d gap(s) for %s", len(gaps), key)
    ingester = DuneIngester(api_key=dune_api_key, data_dir=data_dir)
    for gap_start, gap_end in gaps:
        log.info("Filling gap: blocks %d–%d", gap_start, gap_end)
        ingester.ingest(
            protocol=protocol,
            sql_name=sql_name,
            start_block=gap_start,
            end_block=gap_end,
            chain_id=chain_id,
        )


def fill_with_cryo(
    protocol: str,
    chain_id: int,
    start_block: int,
    end_block: int,
    contract_address: str,
    rpc_url: Optional[str] = None,
    data_dir: Path = Path("data/raw"),
) -> None:
    """Surgical block extraction via cryo for blocks that fail Dune validation.

    Requires cryo CLI (github.com/paradigmxyz/cryo) and an archive RPC URL
    set in ARCHIVE_RPC_URL env var or passed directly.
    """
    rpc = rpc_url or os.environ.get("ARCHIVE_RPC_URL")
    if not rpc:
        raise ValueError(
            "Archive RPC URL required for cryo extraction. "
            "Set ARCHIVE_RPC_URL or pass rpc_url argument."
        )

    out_dir = Path(data_dir) / protocol / f"chain={chain_id}" / "cryo_raw"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "cryo",
        "logs",
        "--blocks",
        f"{start_block}:{end_block}",
        "--contract",
        contract_address,
        "--rpc",
        rpc,
        "--output-dir",
        str(out_dir),
        "--overwrite",
    ]

    log.info("Running cryo: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"cryo failed:\n{result.stderr}")

    log.info("cryo output in %s", out_dir)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _find_gaps(
    sorted_ranges: list[tuple[int, int]],
    global_start: int,
    global_end: int,
) -> list[tuple[int, int]]:
    """Return inclusive (start, end) gaps between sorted ranges."""
    gaps = []
    cursor = global_start
    for start, end in sorted_ranges:
        if start > cursor:
            gaps.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor <= global_end:
        gaps.append((cursor, global_end))
    return gaps


def _check_episode_covered(
    coverage: dict,
    ep_start: int,
    ep_end: int,
) -> bool:
    """Return True if ep_start..ep_end is fully covered in any protocol's data."""
    for info in coverage.values():
        for r_start, r_end in info["covered_ranges"]:
            if r_start <= ep_start and r_end >= ep_end:
                return True
    return False
