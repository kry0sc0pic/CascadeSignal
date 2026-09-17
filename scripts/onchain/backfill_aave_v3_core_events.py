"""Pull Aave v3 Ethereum core events (Supply/Borrow/Repay/Withdraw) via
Etherscan `getLogs`, not Dune.

The Dune query + registry for this already exist
(`scripts/dune/aave_v3_core_events.sql`, `ingest_dune.py --protocol aave_v3
--events core`), but a cost-test measured the full-history pull at ~1M rows /
~4,900 Dune credits against only ~500 available — it prices out. The v3 Pool
emits these events itself, so Etherscan `getLogs` (free, rate-limited only)
gets the same data for free, exactly as `backfill_reserve_index.py` did for
Aave v2's `ReserveDataUpdated`. Output schema matches
`scripts/dune/aave_v3_core_events.sql` / the aave_v2 core-events parquet, so it
drops into `data/raw/aave_v3/` for the (to-be-adapted) v3 state engine.

Aave v3 Pool events (from aave-v3-core `IPool.sol`). Indexed params take topic
slots in signature order; the rest are ABI-packed in `data`:

 Supply(address indexed reserve, address user, address indexed onBehalfOf,
 uint256 amount, uint16 indexed referralCode)
 Withdraw(address indexed reserve, address indexed user, address indexed to,
 uint256 amount)
 Borrow(address indexed reserve, address user, address indexed onBehalfOf,
 uint256 amount, uint8 interestRateMode, uint256 borrowRate,
 uint16 indexed referralCode)
 Repay(address indexed reserve, address indexed user, address indexed
 repayer, uint256 amount, bool useATokens)

The position holder is `onBehalfOf` for Supply/Borrow and `user` for
Repay/Withdraw (the same choice `aave_v3_core_events.sql` makes with
`onBehalfOf AS "user"`), so — unlike Aave v2 — no separate onBehalfOf
correction pass is needed; it's read straight from the indexed topic here.

Validated end-to-end against the Dune cost-test window (blocks 20,000,000–
20,002,000): 235 rows, Supply 78 / Borrow 64 / Withdraw 61 / Repay 32.

Usage:
 python scripts/onchain/backfill_aave_v3_core_events.py # full history
 python scripts/onchain/backfill_aave_v3_core_events.py \
 --start-block 20000000 --end-block 20002000 --out /tmp/v3_probe.parquet
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve.parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve.parent))

from cascadesignal.ingest.schema import to_arrow # noqa: E402
from fetch_svr_feed_events import get_logs_paginated # noqa: E402

V3_POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"

# Per event: topic0 (keccak256 of the canonical signature, in the comment),
# the indexed-topic slot holding the position holder, and the `data` word
# index holding `amount`. `reserve` (the asset) is always indexed topic slot 1.
# topic0s computed once via keccak (pycryptodome) and pinned here -- see the
# probe-window validation in the module docstring, which fails loudly (0 rows
# for a mistyped event) if any is wrong.
_SPECS: dict[str, dict] = {
 # Supply(address,address,address,uint256,uint16)
 "Supply": {
 "topic0": "0x2b627736bca15cd5381dcf80b0bf11fd197d01a037c52b927a881a10fb73ba61",
 "user_topic": 2,
 "amount_word": 1,
 }, # noqa: E501
 # Withdraw(address,address,address,uint256)
 "Withdraw": {
 "topic0": "0x3115d1449a7b732c986cba18244e897a450f61e1bb8d589cd2e69e6c8924f9f7",
 "user_topic": 2,
 "amount_word": 0,
 }, # noqa: E501
 # Borrow(address,address,address,uint256,uint8,uint256,uint16)
 "Borrow": {
 "topic0": "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0",
 "user_topic": 2,
 "amount_word": 1,
 }, # noqa: E501
 # Repay(address,address,address,uint256,bool)
 "Repay": {
 "topic0": "0xa534c8dbe71f871f9f3530e97a74601fea17b426cae02e1c5aee42c96c784051",
 "user_topic": 2,
 "amount_word": 0,
 }, # noqa: E501
}

# Aave v3 Pool on Ethereum deployed at block 16,291,127 (2023-01-27); study
# period ends at block ~24,558,681 (2026-02-28). Same 250k chunking as
# backfill_reserve_index.py; get_logs_paginated bisects any chunk that hits
# Etherscan's 10k-row window cap.
_MIN_BLOCK = 16_291_000
_MAX_BLOCK = 24_600_000
_CHUNK_SIZE = 250_000

_CHECKPOINT_DIR = Path("data/raw/.checkpoints")
_OUT_PARQUET = Path(
 "data/raw/aave_v3/chain=1/aave_v3_core_events_etherscan_full_history.parquet"
)


def _hex_to_int(value: str) -> int:
 return int(value, 16) if value not in ("0x", "", None) else 0


def _decode(event_type: str, logs: list[dict]) -> list[dict]:
 spec = _SPECS[event_type]
 rows = []
 for log in logs:
 data = bytes.fromhex(log["data"][2:])
 word = int.from_bytes(
 data[spec["amount_word"] * 32 : spec["amount_word"] * 32 + 32], "big"
 )
 rows.append(
 {
 "chain_id": 1,
 "block_number": _hex_to_int(log["blockNumber"]),
 "block_timestamp": pd.Timestamp(
 _hex_to_int(log["timeStamp"]), unit="s", tz="UTC"
 ),
 "tx_hash": log["transactionHash"].lower,
 "log_index": _hex_to_int(log["logIndex"]),
 "protocol": "aave_v3",
 "event_type": event_type,
 "user": ("0x" + log["topics"][spec["user_topic"]][-40:]).lower,
 "collateral_asset": None,
 "debt_asset": ("0x" + log["topics"][1][-40:]).lower,
 "amount_raw": str(word),
 "amount_usd": None,
 "liquidator": None,
 "collateral_seized_raw": None,
 "collateral_seized_usd": None,
 }
 )
 return rows


def _chunks -> list[tuple[int, int]]:
 bounds = list(range(_MIN_BLOCK, _MAX_BLOCK, _CHUNK_SIZE)) + [_MAX_BLOCK]
 return [(lo, hi - 1) for lo, hi in zip(bounds[:-1], bounds[1:])]


def _pull(
 event_type: str, lo: int, hi: int, api_key: str, use_ckpt: bool
) -> list[dict]:
 ckpt = _CHECKPOINT_DIR / f"aave_v3_core_{event_type}_{lo}_{hi}.json"
 if use_ckpt and ckpt.exists:
 rows: list[dict] = json.loads(ckpt.read_text)
 print(f" {event_type} [{lo},{hi}]: {len(rows)} rows (checkpoint)", flush=True)
 return rows
 logs = get_logs_paginated(
 V3_POOL,
 api_key,
 from_block=lo,
 to_block=hi,
 topic0=_SPECS[event_type]["topic0"],
 )
 rows = _decode(event_type, logs)
 if use_ckpt:
 _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
 ckpt.write_text(json.dumps(rows, default=str))
 print(f" {event_type} [{lo},{hi}]: {len(rows)} rows", flush=True)
 return rows


def _pull_with_retry(
 event_type: str, lo: int, hi: int, api_key: str, use_ckpt: bool, attempts: int = 4
) -> list[dict]:
 for attempt in range(attempts):
 try:
 return _pull(event_type, lo, hi, api_key, use_ckpt)
 except RuntimeError as exc:
 if attempt == attempts - 1:
 raise
 wait = 30 * (attempt + 1)
 print(
 f" retry {event_type} [{lo},{hi}] in {wait}s after: {exc}", flush=True
 )
 time.sleep(wait)
 raise AssertionError("unreachable") # pragma: no cover


def _finalize(rows: list[dict]) -> pd.DataFrame:
 """Dedupe + sort; `to_arrow` (CANONICAL_SCHEMA) does the typing on write."""
 df = pd.DataFrame(rows).drop_duplicates(subset=["tx_hash", "log_index"])
 # Checkpoints JSON-serialize the Timestamp as a string (json default=str);
 # re-parse so the canonical-schema cast sees datetimes (idempotent when the
 # rows came straight from _decode as Timestamps).
 df["block_timestamp"] = pd.to_datetime(df["block_timestamp"], utc=True)
 return df.sort_values(["block_number", "log_index"], kind="mergesort").reset_index(
 drop=True
 )


def main -> None:
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument("--start-block", type=int, default=_MIN_BLOCK)
 parser.add_argument("--end-block", type=int, default=_MAX_BLOCK)
 parser.add_argument("--out", type=Path, default=_OUT_PARQUET)
 args = parser.parse_args

 api_key = os.environ["ETHERSCAN_API_KEY"]
 # Small probe windows run without checkpoints (validation); the full pull
 # checkpoints per (event, chunk) so a mid-run stall resumes for free.
 full_run = args.start_block == _MIN_BLOCK and args.end_block == _MAX_BLOCK
 if full_run:
 chunks = _chunks
 else:
 chunks = [(args.start_block, args.end_block)]

 print(
 f"Pulling aave_v3 core events over {len(chunks)} chunk(s) "
 f"[{args.start_block}, {args.end_block}]...",
 flush=True,
 )
 all_rows: list[dict] = []
 for event_type in _SPECS:
 for lo, hi in chunks:
 all_rows.extend(_pull_with_retry(event_type, lo, hi, api_key, full_run))

 if not all_rows:
 raise RuntimeError("No aave_v3 core-event logs pulled -- nothing to write")

 df = _finalize(all_rows)
 args.out.parent.mkdir(parents=True, exist_ok=True)
 pq.write_table(to_arrow(df), args.out, compression="zstd")
 print(f"\nWrote {len(df)} aave_v3 core-event rows to {args.out}", flush=True)
 print(df["event_type"].value_counts.to_dict, flush=True)


if __name__ == "__main__":
 main
