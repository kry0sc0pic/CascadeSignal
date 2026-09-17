"""Bootstrap Aave v3 cross-chain event dataset from arXiv 2512.11363.

Primary path: download from Zenodo DOI 10.5281/zenodo.17898640.
The dataset covers 6 EVM chains (Ethereum, Arbitrum, Optimism, Polygon,
Avalanche, Base) from deployment through Oct 2025, with 8 event types.

Fallback: if the Zenodo record is not yet published, ingest Ethereum mainnet
Aave v3 via Dune Analytics (covers the same study period with less breadth).

File layout produced:
 data/raw/aave_v3/chain={chain_id}/blocks_{start:09d}_{end:09d}_arxiv.parquet
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow.parquet as pq
import requests

from cascadesignal.ingest.schema import normalize, to_arrow

log = logging.getLogger(__name__)

ZENODO_DOI = "10.5281/zenodo.17898640"
ZENODO_API = "https://zenodo.org/api/records/{record_id}"

# Mapping: chain name in the arXiv dataset → EVM chain_id
CHAIN_MAP = {
 "ethereum": 1,
 "arbitrum": 42161,
 "optimism": 10,
 "polygon": 137,
 "avalanche": 43114,
 "base": 8453,
}

# Column rename map: arXiv dataset → canonical schema
_ARXIV_COL_MAP = {
 # Common metadata
 "block_number": "block_number",
 "block_timestamp": "block_timestamp",
 "transaction_hash": "tx_hash",
 "log_index": "log_index",
 "event_name": "event_type",
 # LiquidationCall specific
 "collateral_asset": "collateral_asset",
 "debt_asset": "debt_asset",
 "user": "user",
 "debt_to_cover": "amount_raw",
 "debt_to_cover_usd": "amount_usd",
 "liquidator": "liquidator",
 "liquidated_collateral_amount": "collateral_seized_raw",
 "liquidated_collateral_amount_usd": "collateral_seized_usd",
 # Other events: "amount" maps to amount_raw; "amount_usd" maps to amount_usd
 "amount": "amount_raw",
 "amount_usd": "amount_usd",
 # Some events use "reserve" as the debt/supply asset
 "reserve": "debt_asset",
 "on_behalf_of": "user",
}


class ArxivAaveV3Ingestor:
 """Downloads and normalizes the arXiv 2512.11363 Aave v3 dataset.

 If the Zenodo record is unavailable, falls back to Dune Analytics for
 Ethereum mainnet only (the MVP core chain).
 """

 def __init__(
 self,
 data_dir: Path = Path("data/raw"),
 hf_token: Optional[str] = None,
 target_chains: Optional[list[int]] = None,
 ):
 self.data_dir = Path(data_dir)
 self.hf_token = hf_token or os.environ.get("HF_TOKEN")
 # Default: all 6 chains; can restrict for faster dev runs
 self.target_chains = target_chains or list(CHAIN_MAP.values)

 def run(self, dry_run: bool = False) -> dict:
 """Download and normalize the dataset.

 Returns a summary dict with status and row counts per chain.
 """
 zenodo_url = self._resolve_zenodo

 if zenodo_url:
 log.info("Zenodo record found at %s", zenodo_url)
 return self._ingest_zenodo(zenodo_url, dry_run=dry_run)
 else:
 log.warning(
 "Zenodo record %s not yet published. "
 "Run scripts/ingest_dune.py --protocol aave_v3 as fallback.",
 ZENODO_DOI,
 )
 return {"status": "zenodo_unavailable", "doi": ZENODO_DOI, "rows": {}}

 def _resolve_zenodo(self) -> Optional[str]:
 """Check if the Zenodo DOI resolves to a published record.

 Returns the download URL for the dataset archive, or None if not found.
 """
 record_id = ZENODO_DOI.split("zenodo.")[-1]
 try:
 resp = requests.get(
 ZENODO_API.format(record_id=record_id),
 timeout=15,
 )
 if resp.status_code == 404:
 return None
 resp.raise_for_status
 record = resp.json
 # Look for a zip or tar file in the record's files
 for f in record.get("files", []):
 name = f.get("key", "")
 if name.endswith((".zip", ".tar.gz", ".tar")):
 return f["links"]["self"]
 # Fallback: return the record landing page
 return record.get("links", {}).get("html")
 except requests.RequestException as e:
 log.debug("Zenodo API error: %s", e)
 return None

 def _ingest_zenodo(self, archive_url: str, dry_run: bool) -> dict:
 """Download the Zenodo archive, extract parquet files, normalize schema."""
 log.info("Downloading Zenodo archive: %s", archive_url)

 if dry_run:
 log.info("[DRY RUN] Would download %s", archive_url)
 return {"status": "dry_run", "url": archive_url}

 resp = requests.get(archive_url, stream=True, timeout=120)
 resp.raise_for_status

 with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
 parquet_files = [n for n in zf.namelist if n.endswith(".parquet")]
 log.info("Archive contains %d parquet files", len(parquet_files))
 rows_by_chain: dict[int, int] = {}
 for fname in parquet_files:
 chain_id = self._chain_id_from_filename(fname)
 if chain_id not in self.target_chains:
 continue
 with zf.open(fname) as f:
 df = pd.read_parquet(io.BytesIO(f.read))
 n = self._write_normalized(df, chain_id, fname)
 rows_by_chain[chain_id] = rows_by_chain.get(chain_id, 0) + n

 return {"status": "ok", "rows": rows_by_chain}

 def _write_normalized(
 self, df: pd.DataFrame, chain_id: int, source_file: str
 ) -> int:
 """Normalize a raw arXiv parquet file and write to data/raw/aave_v3/."""
 df = df.rename(columns=_ARXIV_COL_MAP)
 df["chain_id"] = chain_id
 df["protocol"] = "aave_v3"
 df = normalize(df, "aave_v3")

 out_dir = self.data_dir / "aave_v3" / f"chain={chain_id}"
 out_dir.mkdir(parents=True, exist_ok=True)

 # Derive a stable output filename from the source file's block range
 stem = Path(source_file).stem
 out_path = out_dir / f"{stem}_arxiv.parquet"
 pq.write_table(to_arrow(df), out_path, compression="zstd")
 log.debug("Wrote %d rows → %s", len(df), out_path)
 return len(df)

 @staticmethod
 def _chain_id_from_filename(filename: str) -> int:
 """Infer chain_id from the filename or directory structure.

 Expected patterns:
 ethereum/LiquidationCall/part001.parquet
 ethereum_LiquidationCall_part001.parquet
 """
 lower = filename.lower
 for chain_name, chain_id in CHAIN_MAP.items:
 if chain_name in lower:
 return chain_id
 return 1 # default to Ethereum


def check_arxiv_status -> dict:
 """Quick check of Zenodo DOI availability. Returns status dict."""
 ingestor = ArxivAaveV3Ingestor
 url = ingestor._resolve_zenodo
 return {
 "doi": ZENODO_DOI,
 "available": url is not None,
 "url": url,
 }
