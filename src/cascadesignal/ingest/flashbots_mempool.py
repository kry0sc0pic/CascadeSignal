"""Flashbots mempool-dumpster ingestion (CAS-25).

Daily raw dumps are ~4-7 GB/day as parquet (full tx bodies) — bulk-pulling
the whole Aug-2023 -> Feb-2026 sub-period that way is infeasible (see
data/raw/provenance/source_status.md row #9). Mempool-dumpster also publishes
a much smaller `.csv.zip` per day (~100 MB compressed / ~300 MB raw) with the
same per-tx metadata minus full calldata — `to` address + 4-byte selector is
enough to identify pending txs aimed at a lending pool contract, which is all
the pending-liquidation-intent signal needs. This module streams that CSV,
filters to rows targeting a known lending-pool contract, and discards the
rest — so disk usage stays at KB/day instead of GB/day.

This is a pending-tx metadata stream, not an on-chain event stream, so it
does NOT use the canonical event schema (src/cascadesignal/ingest/schema.py).

Output:
  data/raw/flashbots_mempool/filtered/{date}.parquet
  data/raw/.checkpoints/flashbots_mempool_days.json
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional

import pandas as pd
import requests

log = logging.getLogger(__name__)

_BASE_URL = "https://mempool-dumpster.flashbots.net/ethereum/mainnet"

# mempool-dumpster's first published day (verified against its own site
# index -- 2023-08-01 through 2023-08-06 404, coverage actually starts 08-07).
COVERAGE_START = date(2023, 8, 7)
# Study period end (configs/ingest.yaml).
STUDY_END = date(2026, 2, 28)

# Both mempool-era golden episodes (China'21/Terra'22/FTX'22 predate
# COVERAGE_START entirely -- see the "honest sub-period flag" below).
# Kept in sync with docs/episodes/oct-2025.md and docs/episodes/feb-2026.md.
GOLDEN_MEMPOOL_WINDOWS = [
    ("Oct 2025 flash deleveraging", date(2025, 10, 8), date(2025, 10, 13)),
    ("Feb 2026 Fed-nomination", date(2026, 1, 29), date(2026, 2, 6)),
]

# Lending-pool contracts to match against the CSV's `to` column. Scoped to
# the project's MVP protocols (Aave v2 + v3) -- both golden episodes in this
# sub-period are Aave-dominated per the episode dossiers.
LENDING_POOL_CONTRACTS: dict[str, str] = {
    "aave_v2": "0x7d2768de32b0b80b7a3454c06bdac94a69ddc7a9",
    "aave_v3": "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2",
}
_ADDRESS_TO_PROTOCOL = {v: k for k, v in LENDING_POOL_CONTRACTS.items()}

_CHUNK_SIZE = 200_000
_CSV_DTYPES = {
    "hash": "string",
    "chain_id": "int32",
    "from": "string",
    "to": "string",
    "data_size": "int32",
    "data_4bytes": "string",
    "tx_type": "int8",
}
# Present in every mempool-dumpster CSV since coverage began.
_CORE_COLS = ["timestamp_ms", "hash", "from", "to", "data_size", "data_4bytes"]
# Added to the schema partway through the study period (inclusion/MEV
# tracking) -- read if present, left null otherwise so output parquet has a
# stable column set across the whole sub-period.
_OPTIONAL_COLS = ["included_at_block_height", "inclusion_delay_ms"]


def is_pre_coverage(day: date) -> bool:
    """True if `day` predates mempool-dumpster's published coverage (Aug 2023)."""
    return day < COVERAGE_START


def daily_csv_zip_url(day: date) -> str:
    month = day.strftime("%Y-%m")
    return f"{_BASE_URL}/{month}/{day.isoformat()}.csv.zip"


def _checkpoint_path(data_dir: Path) -> Path:
    return data_dir / ".checkpoints" / "flashbots_mempool_days.json"


def load_checkpoint(data_dir: Path) -> dict:
    path = _checkpoint_path(data_dir)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save_checkpoint(data_dir: Path, checkpoint: dict) -> None:
    path = _checkpoint_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True))


def download_and_filter_day(
    day: date,
    data_dir: Path = Path("data/raw"),
    session: Optional[requests.Session] = None,
) -> dict:
    """Download one day's mempool CSV, filter to lending-pool txs, and persist.

    Streams the day's `.csv.zip` to a temp file, reads it in chunks (pandas
    infers zip compression from the extension), keeps only rows whose `to`
    matches a tracked lending-pool contract, writes the filtered rows to
    data/raw/flashbots_mempool/filtered/{day}.parquet, and deletes the temp
    download. Returns a summary dict; also updates the checkpoint file.
    """
    session = session or requests.Session()
    url = daily_csv_zip_url(day)
    out_dir = data_dir / "flashbots_mempool" / "filtered"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{day.isoformat()}.parquet"

    summary = {
        "date": day.isoformat(),
        "status": "ok",
        "total_rows": 0,
        "filtered_rows": 0,
        "pre_coverage": is_pre_coverage(day),
    }

    resp = session.get(url, stream=True, timeout=120)
    if resp.status_code == 404:
        summary["status"] = "missing"
        log.warning("No mempool dump for %s (404): %s", day, url)
        return summary
    resp.raise_for_status()

    with NamedTemporaryFile(suffix=".csv.zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        for block in resp.iter_content(chunk_size=1 << 20):
            tmp.write(block)

    try:
        # mempool-dumpster's CSV schema gained columns partway through the
        # study period (see _OPTIONAL_COLS) -- only request columns this
        # particular day's file actually has.
        header_cols = set(pd.read_csv(tmp_path, nrows=0).columns)
        usecols = _CORE_COLS + [c for c in _OPTIONAL_COLS if c in header_cols]

        total_rows = 0
        filtered_chunks = []
        for chunk in pd.read_csv(
            tmp_path,
            usecols=usecols,
            dtype=_CSV_DTYPES,  # type: ignore[arg-type]
            chunksize=_CHUNK_SIZE,
        ):
            total_rows += len(chunk)
            chunk["to"] = chunk["to"].str.lower()
            matched = chunk[chunk["to"].isin(_ADDRESS_TO_PROTOCOL)].copy()
            if not matched.empty:
                matched["pool_protocol"] = matched["to"].map(_ADDRESS_TO_PROTOCOL)
                filtered_chunks.append(matched)

        all_cols = _CORE_COLS + _OPTIONAL_COLS + ["pool_protocol", "timestamp"]
        if filtered_chunks:
            result = pd.concat(filtered_chunks, ignore_index=True)
        else:
            result = pd.DataFrame(columns=usecols + ["pool_protocol"])
        for col in _OPTIONAL_COLS:
            if col not in result.columns:
                result[col] = None
        result["timestamp"] = pd.to_datetime(
            result["timestamp_ms"], unit="ms", utc=True
        )
        result = result[all_cols]
        result.to_parquet(out_path, index=False)
        summary["total_rows"] = total_rows
        summary["filtered_rows"] = len(result)
    finally:
        tmp_path.unlink(missing_ok=True)

    log.info(
        "%s: %d/%d txs targeted a tracked lending pool -> %s",
        day,
        summary["filtered_rows"],
        summary["total_rows"],
        out_path,
    )
    return summary


def backfill(
    start_date: date,
    end_date: date,
    data_dir: Path = Path("data/raw"),
) -> dict:
    """Backfill filtered mempool data for every day in [start_date, end_date].

    Resumable: days already present in the checkpoint are skipped. Any
    failure on a single day (network error, unexpected CSV schema, corrupt
    zip, ...) is logged and skipped rather than aborting the whole range,
    since this call can span years of daily requests unattended.
    """
    checkpoint = load_checkpoint(data_dir)
    session = requests.Session()
    day = start_date
    while day <= end_date:
        key = day.isoformat()
        if key in checkpoint and checkpoint[key]["status"] in ("ok", "missing"):
            day += timedelta(days=1)
            continue
        try:
            summary = download_and_filter_day(day, data_dir=data_dir, session=session)
        except Exception as exc:  # noqa: BLE001 - must not abort a multi-year backfill
            log.warning("Failed to process mempool dump for %s: %s", day, exc)
            summary = {"date": key, "status": "error", "error": str(exc)}
        checkpoint[key] = summary
        _save_checkpoint(data_dir, checkpoint)
        day += timedelta(days=1)
    return checkpoint
