"""The Graph ingestion layer for DEX depth/liquidity.

Pulls daily pool-depth + swap-volume bars for the collateral pools mapped in
`collateral_pools.py`, from three subgraphs on The Graph's decentralized
network:

 - Uniswap v2 (`uniswap-v2-ethereum`): PairDayData (reserve0/reserve1/reserveUSD)
 - Uniswap v3 (`Uniswap-V3`): PoolDayData (liquidity/sqrtPrice/tvlUSD)
 - Curve ("Curve Finance Ethereum", Messari schema): LiquidityPoolDailySnapshot
 (inputTokenBalances/totalValueLockedUSD)

Subgraph IDs and every pool address in `collateral_pools.py` were resolved
live via the connected subgraph MCP (schema introspection + `orderBy:
totalValueLockedUSD/reserveUSD`), not guessed. See that module's docstring
and `data/raw/provenance/source_status.md` row #7.

Architecture mirrors `dune.py`: one paginated pull per pool, cursor-paginated
on the day-bucket field (avoids the ~5,000-row `skip` cap), checkpointed so
re-runs resume instead of re-fetching. Unlike Dune, The Graph's free tier is
metered in *queries*, not compute credits (100k/mo) -- `QueryBudget` tracks
local call count against that so a long backfill can't silently blow the
monthly allowance.

Requires a working `GRAPH_API_KEY` (create/verify at
https://thegraph.com/studio/apikeys/) -- see `.env.example`. Exercised
end-to-end 2026-07-15 for the full study period (all 17 pools, 91 queries
against the 100k/mo free tier) -- see `data/raw/provenance/source_status.md`
row #7. A newly-created key returned `"auth error: API key not found"` for
roughly an hour before it started authenticating (activation lag on The
Graph's side, not a code/config issue) -- if a fresh key 401s immediately
after creation, that's the likely cause, not a bug here.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

from cascadesignal.ingest.collateral_pools import COLLATERAL_POOLS

log = logging.getLogger(__name__)

_GATEWAY_BASE = "https://gateway.thegraph.com/api"
_PAGE_SIZE = 1000
_MONTHLY_QUERY_LIMIT = 100_000
_QUERY_BUDGET_SAFETY_MARGIN = 5_000 # stop at 95k, not the full 100k

# Subgraph IDs (The Graph Network curation IDs, stable across subgraph
# upgrades) -- confirmed live via mcp__subgraph__search_subgraphs_by_keyword
# + get_deployment_30day_query_counts on 2026-07-14 (33.2M / 12.7M / 174k
# queries in the trailing 30 days respectively, i.e. the actively-indexed
# deployments, not a stale/abandoned fork).
SUBGRAPH_IDS = {
 "uniswap_v2": "GmSczqdCDZ3hJeYY9JphwsADn5rePUzUKm8EZcVuhRAm",
 "uniswap_v3": "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV",
 "curve": "3fy93eAT56UJsRCEht8iFhfi6wjHWXtZ9dnnbQmvFopF",
}

STUDY_START_TS = 1_609_459_200 # 2021-01-01 00:00 UTC
STUDY_END_TS = 1_772_323_199 # 2026-02-28 23:59 UTC

UNISWAP_V2_SCHEMA = pa.schema(
 [
 pa.field("chain_id", pa.int32),
 pa.field("dex", pa.string),
 pa.field("pool_address", pa.string),
 pa.field("collateral_symbol", pa.string),
 pa.field("date", pa.int64),
 pa.field("reserve0", pa.float64),
 pa.field("reserve1", pa.float64),
 pa.field("reserve_usd", pa.float64),
 pa.field("daily_volume_token0", pa.float64),
 pa.field("daily_volume_token1", pa.float64),
 pa.field("daily_volume_usd", pa.float64),
 ]
)

UNISWAP_V3_SCHEMA = pa.schema(
 [
 pa.field("chain_id", pa.int32),
 pa.field("dex", pa.string),
 pa.field("pool_address", pa.string),
 pa.field("collateral_symbol", pa.string),
 pa.field("fee_tier", pa.int32),
 pa.field("date", pa.int64),
 pa.field("liquidity", pa.string), # uint128, string to avoid overflow
 pa.field("sqrt_price", pa.string), # uint160, string to avoid overflow
 pa.field("token0_price", pa.float64),
 pa.field("token1_price", pa.float64),
 pa.field("tick", pa.int64),
 pa.field("tvl_usd", pa.float64),
 pa.field("daily_volume_token0", pa.float64),
 pa.field("daily_volume_token1", pa.float64),
 pa.field("daily_volume_usd", pa.float64),
 ]
)

CURVE_SCHEMA = pa.schema(
 [
 pa.field("chain_id", pa.int32),
 pa.field("dex", pa.string),
 pa.field("pool_address", pa.string),
 pa.field("collateral_symbol", pa.string),
 pa.field("date", pa.int64),
 pa.field("input_token_balances_raw", pa.string), # JSON list, uint256-safe
 pa.field("tvl_usd", pa.float64),
 pa.field("daily_volume_usd", pa.float64),
 ]
)

_QUERIES = {
 "uniswap_v2": """
 query($addr: Bytes!, $start: Int!, $end: Int!) {
 pairDayDatas(first: %d, orderBy: date, orderDirection: asc,
 where: {pairAddress: $addr, date_gte: $start, date_lte: $end}) {
 date reserve0 reserve1 reserveUSD
 dailyVolumeToken0 dailyVolumeToken1 dailyVolumeUSD
 }
 }
 """
 % _PAGE_SIZE,
 "uniswap_v3": """
 query($addr: String!, $start: Int!, $end: Int!) {
 poolDayDatas(first: %d, orderBy: date, orderDirection: asc,
 where: {pool: $addr, date_gte: $start, date_lte: $end}) {
 date liquidity sqrtPrice token0Price token1Price tick tvlUSD
 volumeToken0 volumeToken1 volumeUSD
 }
 }
 """
 % _PAGE_SIZE,
 "curve": """
 query($addr: String!, $start: Int!, $end: Int!) {
 liquidityPoolDailySnapshots(first: %d, orderBy: timestamp, orderDirection: asc,
 where: {pool: $addr, timestamp_gte: $start, timestamp_lte: $end}) {
 timestamp totalValueLockedUSD dailyVolumeUSD inputTokenBalances
 }
 }
 """
 % _PAGE_SIZE,
}

_ROW_KEY = {
 "uniswap_v2": "pairDayDatas",
 "uniswap_v3": "poolDayDatas",
 "curve": "liquidityPoolDailySnapshots",
}


class QueryBudget:
 """Tracks GraphQL query count against The Graph's 100k free queries/mo.

 Resets automatically at the start of each calendar month (matching how
 the free tier itself resets), so a session started in a new month isn't
 stuck thinking last month's count still applies.
 """

 def __init__(self, path: Path):
 self.path = path
 self.path.parent.mkdir(parents=True, exist_ok=True)
 self._state = self._load

 def _load(self) -> dict:
 if self.path.exists:
 state = json.loads(self.path.read_text)
 if state.get("month") == self._current_month:
 return state
 return {"month": self._current_month, "queries": 0}

 @staticmethod
 def _current_month -> str:
 return time.strftime("%Y-%m", time.gmtime)

 def remaining(self) -> int:
 return (
 _MONTHLY_QUERY_LIMIT - _QUERY_BUDGET_SAFETY_MARGIN - self._state["queries"]
 )

 def record(self, n: int = 1) -> None:
 self._state = self._load # pick up month rollover
 self._state["queries"] += n
 self.path.write_text(json.dumps(self._state))

 def check(self) -> None:
 if self.remaining <= 0:
 raise RuntimeError(
 f"The Graph query budget exhausted for {self._state['month']} "
 f"({self._state['queries']} queries used, "
 f"{_QUERY_BUDGET_SAFETY_MARGIN} safety margin held back from "
 f"the {_MONTHLY_QUERY_LIMIT}/mo free-tier limit)."
 )


class TheGraphIngester:
 """Downloads DEX pool-depth/swap-volume day bars into partitioned parquet."""

 def __init__(
 self,
 api_key: Optional[str] = None,
 data_dir: Path = Path("data/raw"),
 ):
 self.api_key = api_key or os.environ.get("GRAPH_API_KEY")
 if not self.api_key:
 raise ValueError("GRAPH_API_KEY env var or api_key argument is required")
 self.data_dir = Path(data_dir)
 self._checkpoint_dir = self.data_dir / ".checkpoints"
 self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
 self.budget = QueryBudget(self._checkpoint_dir / "thegraph_query_budget.json")

 # ------------------------------------------------------------------
 # Public API
 # ------------------------------------------------------------------

 def ingest_pool(
 self,
 dex: str,
 pool_address: str,
 collateral_symbol: str,
 chain_id: int = 1,
 start_ts: int = STUDY_START_TS,
 end_ts: int = STUDY_END_TS,
 fee_tier: Optional[int] = None,
 ) -> int:
 """Fetch all day bars for one pool over [start_ts, end_ts].

 Cursor-paginates on the date/timestamp field so it isn't bounded by
 The Graph's ~5,000-row `skip` cap. Resumes from a checkpoint if a
 prior run got partway through. Returns total rows written.
 """
 cp_key = f"{dex}_{pool_address}"
 cp = self._load_checkpoint(cp_key)
 cursor = cp.get("last_date", start_ts - 1) + 1
 rows: list[dict] = cp.get("_pending_rows", [])

 while True:
 self.budget.check
 batch = self._query_page(dex, pool_address, cursor, end_ts)
 self.budget.record
 if not batch:
 break
 rows.extend(batch)
 cursor = self._row_date(dex, batch[-1]) + 1
 self._save_checkpoint(cp_key, {"last_date": cursor - 1})
 if len(batch) < _PAGE_SIZE:
 break

 if not rows:
 log.info("%s/%s: no data in range", dex, pool_address)
 return 0

 df = self._normalize(
 dex, rows, pool_address, collateral_symbol, chain_id, fee_tier
 )
 out_dir = self.data_dir / "thegraph" / dex / f"chain={chain_id}"
 out_dir.mkdir(parents=True, exist_ok=True)
 # Keyed by pool address alone, not by collateral symbol: a pool can
 # be the canonical venue for two different collateral assets at
 # once (e.g. the USDC/WETH pool is both USDC's and WETH's entry in
 # `collateral_pools.py`), so a symbol-prefixed filename would either
 # collide or silently only exist under whichever symbol ingested
 # first. Callers resolve a symbol to its file via
 # `collateral_pools.COLLATERAL_POOLS[symbol]`, not by filename.
 out_path = out_dir / f"{pool_address}.parquet"
 pq.write_table(
 pa.Table.from_pandas(df, schema=self._schema(dex), safe=False),
 out_path,
 compression="zstd",
 )
 log.info("Wrote %d rows -> %s", len(df), out_path)
 return len(df)

 def ingest_all(
 self, start_ts: int = STUDY_START_TS, end_ts: int = STUDY_END_TS
 ) -> dict[str, int]:
 """Ingest every mapped pool in `collateral_pools.COLLATERAL_POOLS`."""
 totals: dict[str, int] = {}
 for symbol, mapping in COLLATERAL_POOLS.items:
 if mapping.uniswap_v2_pair:
 totals[f"{symbol}_v2"] = self.ingest_pool(
 "uniswap_v2",
 mapping.uniswap_v2_pair,
 symbol,
 start_ts=start_ts,
 end_ts=end_ts,
 )
 if mapping.uniswap_v3_pool:
 totals[f"{symbol}_v3"] = self.ingest_pool(
 "uniswap_v3",
 mapping.uniswap_v3_pool,
 symbol,
 start_ts=start_ts,
 end_ts=end_ts,
 fee_tier=mapping.uniswap_v3_fee_tier,
 )
 if mapping.curve_pool:
 totals[f"{symbol}_curve"] = self.ingest_pool(
 "curve",
 mapping.curve_pool,
 symbol,
 start_ts=start_ts,
 end_ts=end_ts,
 )
 return totals

 # ------------------------------------------------------------------
 # Query execution
 # ------------------------------------------------------------------

 def _query_page(self, dex: str, addr: str, start: int, end: int) -> list[dict]:
 url = f"{_GATEWAY_BASE}/{self.api_key}/subgraphs/id/{SUBGRAPH_IDS[dex]}"
 resp = self._request(
 url,
 {
 "query": _QUERIES[dex],
 "variables": {"addr": addr, "start": start, "end": end},
 },
 )
 payload = resp.json
 if "errors" in payload:
 raise RuntimeError(
 f"The Graph query error ({dex}/{addr}): {payload['errors']}"
 )
 return payload["data"][_ROW_KEY[dex]]

 def _request(self, url: str, body: dict, max_retries: int = 5) -> requests.Response:
 backoff = 2.0
 resp = None
 for attempt in range(max_retries + 1):
 resp = requests.post(url, json=body, timeout=30)
 if resp.status_code == 429 or resp.status_code >= 500:
 if attempt == max_retries:
 break
 wait = float(resp.headers.get("Retry-After", backoff))
 log.warning("The Graph %d; backing off %.1fs", resp.status_code, wait)
 time.sleep(wait)
 backoff = min(backoff * 2, 60.0)
 continue
 resp.raise_for_status
 return resp
 assert resp is not None
 resp.raise_for_status
 return resp

 @staticmethod
 def _row_date(dex: str, row: dict) -> int:
 # Curve's Messari schema declares `timestamp` as BigInt, which The
 # Graph serializes as a JSON string (unlike Uniswap's plain `Int`
 # `date` field) -- cast explicitly so cursor pagination doesn't try
 # to add 1 to a string.
 return int(row["timestamp"]) if dex == "curve" else int(row["date"])

 # ------------------------------------------------------------------
 # Normalization
 # ------------------------------------------------------------------

 @staticmethod
 def _schema(dex: str) -> pa.Schema:
 return {
 "uniswap_v2": UNISWAP_V2_SCHEMA,
 "uniswap_v3": UNISWAP_V3_SCHEMA,
 "curve": CURVE_SCHEMA,
 }[dex]

 @staticmethod
 def _normalize(
 dex: str,
 rows: list[dict],
 pool_address: str,
 collateral_symbol: str,
 chain_id: int,
 fee_tier: Optional[int],
 ) -> pd.DataFrame:
 df = pd.DataFrame(rows)
 df["chain_id"] = chain_id
 df["dex"] = dex
 df["pool_address"] = pool_address
 df["collateral_symbol"] = collateral_symbol

 if dex == "uniswap_v2":
 df = df.rename(
 columns={
 "reserveUSD": "reserve_usd",
 "dailyVolumeToken0": "daily_volume_token0",
 "dailyVolumeToken1": "daily_volume_token1",
 "dailyVolumeUSD": "daily_volume_usd",
 }
 )
 for col in (
 "reserve0",
 "reserve1",
 "reserve_usd",
 "daily_volume_token0",
 "daily_volume_token1",
 "daily_volume_usd",
 ):
 df[col] = df[col].astype("float64")
 df["date"] = df["date"].astype("int64")
 elif dex == "uniswap_v3":
 df["fee_tier"] = fee_tier
 df = df.rename(
 columns={
 "sqrtPrice": "sqrt_price",
 "token0Price": "token0_price",
 "token1Price": "token1_price",
 "tvlUSD": "tvl_usd",
 "volumeToken0": "daily_volume_token0",
 "volumeToken1": "daily_volume_token1",
 "volumeUSD": "daily_volume_usd",
 }
 )
 df["liquidity"] = df["liquidity"].astype(str)
 df["sqrt_price"] = df["sqrt_price"].astype(str)
 for col in (
 "token0_price",
 "token1_price",
 "tvl_usd",
 "daily_volume_token0",
 "daily_volume_token1",
 "daily_volume_usd",
 ):
 df[col] = df[col].astype("float64")
 # tick is nullable in the subgraph schema (a day with no swaps
 # has no tick reading) -- pandas' plain int64 can't hold NaN, so
 # use the nullable Int64 extension dtype.
 df["tick"] = df["tick"].astype("Int64")
 df["date"] = df["date"].astype("int64")
 else: # curve
 df = df.rename(
 columns={
 "timestamp": "date",
 "totalValueLockedUSD": "tvl_usd",
 "dailyVolumeUSD": "daily_volume_usd",
 }
 )
 df["input_token_balances_raw"] = df["inputTokenBalances"].apply(json.dumps)
 df = df.drop(columns=["inputTokenBalances"])
 df["tvl_usd"] = df["tvl_usd"].astype("float64")
 df["daily_volume_usd"] = df["daily_volume_usd"].astype("float64")
 df["date"] = df["date"].astype("int64")

 ordered = [f.name for f in TheGraphIngester._schema(dex)]
 return df[ordered]

 # ------------------------------------------------------------------
 # Checkpoints
 # ------------------------------------------------------------------

 def _checkpoint_path(self, key: str) -> Path:
 return self._checkpoint_dir / f"thegraph_{key}.json"

 def _load_checkpoint(self, key: str) -> dict:
 path = self._checkpoint_path(key)
 return json.loads(path.read_text) if path.exists else {}

 def _save_checkpoint(self, key: str, data: dict) -> None:
 self._checkpoint_path(key).write_text(json.dumps(data))
