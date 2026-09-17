"""Canonical event schema for the CascadeSignal data lake.

All ingestion sources (arXiv Aave v3, Dune, cryo) normalize to this schema
before writing parquet files under data/raw/.

Output partition layout:
 data/raw/{protocol}/chain={chain_id}/blocks_{start:09d}_{end:09d}.parquet
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa

# 8 event types from arXiv 2512.11363 (Aave v3); extended with Aave v2 / Compound / Maker variants
EVENT_TYPES = frozenset(
 {
 # Aave v2/v3
 "Supply", # Aave v3 deposit
 "Deposit", # Aave v2 deposit
 "Borrow",
 "Repay",
 "Withdraw",
 "LiquidationCall",
 "FlashLoan",
 "ReserveDataUpdated",
 "MintedToTreasury",
 # Compound v2
 "Mint", # supply
 "Redeem", # withdraw
 "LiquidateBorrow",
 # Compound v3
 "Absorb", # Comet's actual liquidation event name
 "Supply", # Compound v3 supply (same name as Aave v3)
 "Withdraw", # Compound v3 withdraw
 # Maker
 "Bite", # Cat (old liquidation)
 "Bark", # Dog (new liquidation 2.0)
 }
)

CANONICAL_SCHEMA = pa.schema(
 [
 pa.field("chain_id", pa.int32),
 pa.field("block_number", pa.int64),
 pa.field("block_timestamp", pa.timestamp("us", tz="UTC")),
 pa.field("tx_hash", pa.string),
 pa.field("log_index", pa.int32),
 pa.field("protocol", pa.string),
 pa.field("event_type", pa.string),
 pa.field("user", pa.string),
 # Nullable fields — only populated for liquidation / transfer events
 pa.field("collateral_asset", pa.string),
 pa.field("debt_asset", pa.string),
 # Raw amounts stored as strings to avoid uint256 overflow
 pa.field("amount_raw", pa.string),
 pa.field("amount_usd", pa.float64),
 pa.field("liquidator", pa.string),
 pa.field("collateral_seized_raw", pa.string),
 pa.field("collateral_seized_usd", pa.float64),
 ]
)

_NULLABLE_COLS = {
 "collateral_asset",
 "debt_asset",
 "amount_raw",
 "amount_usd",
 "liquidator",
 "collateral_seized_raw",
 "collateral_seized_usd",
}

_REQUIRED_COLS = [f.name for f in CANONICAL_SCHEMA if f.name not in _NULLABLE_COLS]


def normalize(df: pd.DataFrame, protocol: str) -> pd.DataFrame:
 """Ensure df conforms to the canonical schema.

 Adds missing nullable columns as None, casts types, and lowercases
 address columns. Returns a new DataFrame ordered by canonical column order.

 Args:
 df: Raw DataFrame from any ingestion source.
 protocol: Protocol identifier string (e.g. "aave_v2").

 Returns:
 DataFrame with canonical column order and types.
 """
 df = df.copy

 if "protocol" not in df.columns:
 df["protocol"] = protocol

 # Ensure address columns are lowercase strings
 for col in ("tx_hash", "user", "collateral_asset", "debt_asset", "liquidator"):
 if col in df.columns:
 df[col] = df[col].where(df[col].isna, df[col].str.lower)

 # Coerce numeric types
 if "chain_id" in df.columns:
 df["chain_id"] = df["chain_id"].astype("int32")
 if "block_number" in df.columns:
 df["block_number"] = df["block_number"].astype("int64")
 if "log_index" in df.columns:
 df["log_index"] = df["log_index"].astype("int32")

 # Coerce timestamp to UTC
 if "block_timestamp" in df.columns:
 df["block_timestamp"] = pd.to_datetime(df["block_timestamp"], utc=True)

 # Fill missing nullable columns
 for col in _NULLABLE_COLS:
 if col not in df.columns:
 df[col] = None

 # Return columns in canonical order
 ordered_cols = [f.name for f in CANONICAL_SCHEMA]
 return df[[c for c in ordered_cols if c in df.columns]]


def to_arrow(df: pd.DataFrame) -> pa.Table:
 """Convert a normalized DataFrame to a PyArrow table with the canonical schema."""
 return pa.Table.from_pandas(df, schema=CANONICAL_SCHEMA, safe=False)
