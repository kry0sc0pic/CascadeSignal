"""Pull Aave v2 AaveOracle's full-history `AssetSourceUpdated` events (Lever 11).

The repo's asset -> Chainlink feed maps (`chainlink_feeds.RESERVE_CHAINLINK_FEEDS`/
`RESERVE_CHAINLINK_ETH_FEEDS`) are built by *description-matching*
sweeping known aggregator addresses and matching `"<SYMBOL> / USD"` or
`"<SYMBOL> / ETH"` strings (see `map_chainlink_reserves.py`/
`map_chainlink_eth_feeds.py`). That approach can only ever find feeds that
exist and are named the way we expect; it has no way to confirm that a given
feed is the one Aave's own oracle actually read *at a given historical
block*, or to notice when Aave pointed a reserve at something else entirely
(a custom adapter, not a plain Chainlink aggregator). `fetch_aave_v2_price_oracle_sources.py`
already reads the current (`"latest"`-block) source per reserve via
`getSourceOfAsset`, but Aave v2 has been live since late 2020 and sources can
change over time (`AaveOracle.setAssetSources`) -- a present-day snapshot
says nothing about which source was in effect during the 2021-2022 study
period this project's liquidations fall in.

`AaveOracle` emits, on every source (re)assignment:

 event AssetSourceUpdated(address indexed asset, address indexed source);

Both `asset` and `source` are indexed (no non-indexed `data` word), so the
full event is recoverable from `topics` alone. `topic0`
(`keccak256("AssetSourceUpdated(address,address)")`) was derived via
`Crypto.Hash.keccak` (not memorized) and verified live before use: a real
`getLogs` call against this topic0 + the oracle address returned decodable
rows whose `asset` topics matched well-known mainnet token addresses (DAI
`0x6B175474...`, LINK `0x51491077...`, BAT `0x0D8775F6...`) at block
11,275,902 -- Nov 2020, *before* Aave v2's commonly-cited ~11,362,000 launch
block, meaning the oracle's initial sources were configured ahead of the
pool going live. `_MIN_BLOCK` below is set well under that to not miss it.

`AAVE_ORACLE` (`0xA50ba011c48153De246E5192C8f9258A2ba79Ca9`) reuses the
address already verified two ways in `fetch_aave_v2_price_oracle_sources.py`
(matches `AaveV2Ethereum.sol` in the aave-address-book repo, and
`getPriceOracle` on `POOL_ADDRESSES_PROVIDER` returns it live).

Low-frequency governance event (order of a few hundred rows across 37
reserves over 5 years, not the millions `ReserveDataUpdated` emits), same
cost profile as `backfill_reserve_config_history.py`'s
`CollateralConfigurationChanged` pull -- chunked + per-chunk-checkpointed the
same way for resumability, even though a single unchunked
`get_logs_paginated` call would likely also finish quickly via its own
recursive-bisection cap handling.

Writes `data/raw/aave_v2_oracle_sources/chain=1/asset_source_updated.parquet`
(chain_id, block_number, block_timestamp, log_index, asset, source -- all
lowercased addresses).

Usage:
 python scripts/onchain/backfill_aave_oracle_sources.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve.parent))

from fetch_svr_feed_events import get_logs_paginated # noqa: E402

RPC_URL = "https://ethereum-rpc.publicnode.com"

# Verified live -- see fetch_aave_v2_price_oracle_sources.py's module docstring.
AAVE_ORACLE = "0xA50ba011c48153De246E5192C8f9258A2ba79Ca9"

# keccak256("AssetSourceUpdated(address,address)") -- verified live against a
# real Etherscan getLogs result (see module docstring for the specific hit).
ASSET_SOURCE_UPDATED_TOPIC0 = (
 "0x22c5b7b2d8561d39f7f210b6b326a1aa69f15311163082308ac4877db6339dc1"
)

# Earliest known real AssetSourceUpdated is block 11,275,902 -- floor set
# comfortably below that (see module docstring).
_MIN_BLOCK = 11_000_000
_CHUNK_SIZE = 2_000_000

_CHECKPOINT_DIR = Path("data/raw/.checkpoints")
_OUT_PARQUET = Path(
 "data/raw/aave_v2_oracle_sources/chain=1/asset_source_updated.parquet"
)


def get_block_number -> int:
 payload: dict[str, Any] = {
 "jsonrpc": "2.0",
 "method": "eth_blockNumber",
 "params": [],
 "id": 1,
 }
 resp = requests.post(RPC_URL, json=payload, timeout=20)
 return int(resp.json["result"], 16)


def _hex_to_int(value: str) -> int:
 return int(value, 16) if value not in ("0x", "", None) else 0


def _chunks(max_block: int) -> list[tuple[int, int]]:
 bounds = list(range(_MIN_BLOCK, max_block, _CHUNK_SIZE)) + [max_block]
 return [(lo, hi - 1) for lo, hi in zip(bounds[:-1], bounds[1:])]


def _decode(logs: list[dict]) -> list[dict]:
 rows = []
 for log in logs:
 rows.append(
 {
 "chain_id": 1,
 "block_number": _hex_to_int(log["blockNumber"]),
 "block_timestamp": pd.Timestamp(
 _hex_to_int(log["timeStamp"]), unit="s", tz="UTC"
 ),
 "log_index": _hex_to_int(log["logIndex"]),
 "asset": ("0x" + log["topics"][1][-40:]).lower,
 "source": ("0x" + log["topics"][2][-40:]).lower,
 }
 )
 return rows


def _pull_chunk(lo: int, hi: int, api_key: str) -> list[dict]:
 checkpoint = _CHECKPOINT_DIR / f"aave_oracle_sources_{lo}_{hi}.json"
 if checkpoint.exists:
 rows: list[dict] = json.loads(checkpoint.read_text)
 print(f" [{lo},{hi}]: {len(rows)} rows (checkpoint)", flush=True)
 return rows

 logs = get_logs_paginated(
 AAVE_ORACLE,
 api_key,
 from_block=lo,
 to_block=hi,
 topic0=ASSET_SOURCE_UPDATED_TOPIC0,
 )
 rows = _decode(logs)
 _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
 checkpoint.write_text(json.dumps(rows, default=str))
 print(f" [{lo},{hi}]: {len(rows)} rows (checkpointed)", flush=True)
 return rows


def _pull_chunk_with_retry(
 lo: int, hi: int, api_key: str, attempts: int = 4
) -> list[dict]:
 for attempt in range(attempts):
 try:
 return _pull_chunk(lo, hi, api_key)
 except RuntimeError as exc:
 if attempt == attempts - 1:
 raise
 wait = 30 * (attempt + 1)
 print(f" retry [{lo},{hi}] in {wait}s after: {exc}", flush=True)
 time.sleep(wait)
 raise AssertionError("unreachable") # pragma: no cover


def main -> None:
 api_key = os.environ["ETHERSCAN_API_KEY"]
 chain_tip = get_block_number
 chunks = _chunks(chain_tip)
 print(
 f"Pulling AssetSourceUpdated over {len(chunks)} block chunks "
 f"[{_MIN_BLOCK}, {chain_tip}] (chain tip)...",
 flush=True,
 )

 all_rows: list[dict] = []
 for lo, hi in chunks:
 all_rows.extend(_pull_chunk_with_retry(lo, hi, api_key))

 if not all_rows:
 raise RuntimeError("No AssetSourceUpdated logs pulled")

 df = pd.DataFrame(all_rows)
 df = df.drop_duplicates(subset=["block_number", "log_index", "asset"])
 df["block_number"] = df["block_number"].astype("int64")
 df["chain_id"] = df["chain_id"].astype("int32")
 df["block_timestamp"] = pd.to_datetime(df["block_timestamp"], utc=True)
 df = df.sort_values(["asset", "block_number"], kind="mergesort").reset_index(
 drop=True
 )
 _OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
 df.to_parquet(_OUT_PARQUET, index=False)
 print(f"\nWrote {len(df)} AssetSourceUpdated rows to {_OUT_PARQUET}")
 print(f"Distinct assets with a recorded source change: {df['asset'].nunique}")


if __name__ == "__main__":
 main
