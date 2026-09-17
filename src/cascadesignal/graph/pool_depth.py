"""DEX pool-depth as-of lookup, built on The Graph ingestion.

`ingest/thegraph.py` writes three different per-dex schemas (Uniswap v2's
`reserve_usd`, Uniswap v3's `tvl_usd`, Curve's `tvl_usd`) under
`data/raw/thegraph/{dex}/chain={chain_id}/{pool_address}.parquet`. This
module normalizes all three to one common `depth_usd` column and exposes a
causal, nearest-prior-day lookup -- the same "as-of" convention used
elsewhere in this project's pipelines -- so `build.py`'s POOL_DEPTH edges
never see a pool's future TVL.
"""

from __future__ import annotations

import glob
from pathlib import Path

import pandas as pd

_DEFAULT_DATA_DIR = Path("data/raw/thegraph")

_DEPTH_COL = {
 "uniswap_v2": "reserve_usd",
 "uniswap_v3": "tvl_usd",
 "curve": "tvl_usd",
}

POOL_BARS_COLUMNS = ["dex", "pool_address", "collateral_symbol", "date", "depth_usd"]


def load_pool_depth_bars(
 data_dir: str | Path = _DEFAULT_DATA_DIR, chain_id: int = 1
) -> pd.DataFrame:
 """Load every ingested pool's daily depth bars into one common frame.

 Returns columns `POOL_BARS_COLUMNS`, `date` as a day-resolution UTC
 timestamp. Empty (not missing) if no thegraph data has been ingested yet.
 """
 data_dir = Path(data_dir)
 parts: list[pd.DataFrame] = []
 for dex, depth_col in _DEPTH_COL.items:
 for path in sorted(
 glob.glob(str(data_dir / dex / f"chain={chain_id}" / "*.parquet"))
 ):
 df = pd.read_parquet(path)
 parts.append(
 pd.DataFrame(
 {
 "dex": dex,
 "pool_address": df["pool_address"],
 "collateral_symbol": df["collateral_symbol"],
 "date": pd.to_datetime(df["date"], unit="s", utc=True),
 "depth_usd": df[depth_col].astype("float64"),
 }
 )
 )
 if not parts:
 return pd.DataFrame(columns=POOL_BARS_COLUMNS)
 return pd.concat(parts, ignore_index=True)[POOL_BARS_COLUMNS]


def depth_at(pool_bars: pd.DataFrame, timestamp: pd.Timestamp) -> pd.DataFrame:
 """Each pool's nearest-prior-day depth at-or-before `timestamp`.

 One row per `pool_address` (the deepest-dated bar <= `timestamp`); pools
 with no bar at-or-before `timestamp` yet (not deployed / not ingested
 that far back) are dropped, not zero-filled -- a pool absent from a
 snapshot means "no depth data available", not "zero liquidity".
 """
 if pool_bars.empty:
 return pool_bars

 eligible = pool_bars[pool_bars["date"] <= timestamp]
 if eligible.empty:
 return pool_bars.iloc[0:0]
 idx = eligible.groupby("pool_address")["date"].idxmax
 return eligible.loc[idx].reset_index(drop=True)


__all__ = ["load_pool_depth_bars", "depth_at", "POOL_BARS_COLUMNS"]
