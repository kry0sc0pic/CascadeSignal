"""Decode Aave `LiquidationCall` logs into the canonical event schema.

`LiquidationCall(address indexed collateralAsset, address indexed debtAsset,
address indexed user, uint256 debtToCover, uint256 liquidatedCollateralAmount,
address liquidator, bool receiveAToken)` -- identical ABI on v2 and v3 (same
topic0, confirmed live: querying the v3 Pool with v2's pinned topic0 on
2026-08-04 returned real 128-byte-data LiquidationCall logs). Topics: [sig,
collateralAsset, debtAsset, user]. Data words: [debtToCover,
liquidatedCollateralAmount, liquidator, receiveAToken].

Contract addresses and topic0 match what's already pinned elsewhere in this
repo (`configs/ingest.yaml`, `scripts/onchain/backfill_aave_v2_launch_events.py`,
`scripts/onchain/backfill_aave_v3_core_events.py`) -- not re-derived here.
"""

from __future__ import annotations

import pandas as pd

LIQUIDATION_CALL_TOPIC0 = (
 "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"
)

AAVE_V2_LENDING_POOL = "0x7d2768de32b0b80b7a3454c06bdac94a69ddc7a9"
AAVE_V3_POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"

PROTOCOL_ADDRESSES = {
 "aave_v2": AAVE_V2_LENDING_POOL,
 "aave_v3": AAVE_V3_POOL,
}


def _hex_to_int(value: str | None) -> int:
 return int(value, 16) if value not in (None, "0x", "") else 0


def _addr(topic_or_word_hex: str) -> str:
 return ("0x" + topic_or_word_hex[-40:]).lower


def _words(data_hex: str) -> list[int]:
 data = bytes.fromhex(data_hex[2:])
 return [int.from_bytes(data[i : i + 32], "big") for i in range(0, len(data), 32)]


def decode_liquidation_log(log: dict, protocol: str) -> dict:
 """One raw `eth_getLogs`/`eth_subscribe` log dict -> one canonical-schema
 row (matching `data/raw/<protocol>/chain=1/*.parquet`'s columns), so live
 rows are structurally interchangeable with the historical lake."""
 topics = log["topics"]
 words = _words(log["data"])
 block_number = _hex_to_int(log.get("blockNumber"))
 return {
 "chain_id": 1,
 "block_number": block_number,
 # Etherscan's getLogs includes `timeStamp`; eth_subscribe log pushes
 # don't carry a block timestamp at all, so this stays None for the
 # "pending" live feed (only block_number is authoritative yet). ISO
 # string, not pd.Timestamp/NaT, since these rows are served directly
 # as JSON by live/app.py, not written to parquet.
 "block_timestamp": (
 pd.Timestamp(_hex_to_int(log["timeStamp"]), unit="s", tz="UTC").isoformat
 if log.get("timeStamp")
 else None
 ),
 "tx_hash": log["transactionHash"].lower,
 "log_index": _hex_to_int(log.get("logIndex")),
 "protocol": protocol,
 "event_type": "LiquidationCall",
 "user": _addr(topics[3]),
 "collateral_asset": _addr(topics[1]),
 "debt_asset": _addr(topics[2]),
 "amount_raw": str(words[0]),
 "amount_usd": None,
 "liquidator": _addr(f"{words[2]:064x}"),
 "collateral_seized_raw": str(words[1]),
 "collateral_seized_usd": None,
 }


__all__ = [
 "LIQUIDATION_CALL_TOPIC0",
 "AAVE_V2_LENDING_POOL",
 "AAVE_V3_POOL",
 "PROTOCOL_ADDRESSES",
 "decode_liquidation_log",
]
