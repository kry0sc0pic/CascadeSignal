"""Full-history event pull for xSUSHI's real Aave oracle adapter (Lever 11c).

xSUSHI's original Aave source (`XSushiPriceAdapter`,
`0x9b26214bec078e68a394aaebfbfff406ce14893f`, confirmed via `getsourcecode`
in Lever 11b) computes:

 exchangeRate = SUSHI.balanceOf(xSUSHI) * 1 ether / xSUSHI.totalSupply
 latestAnswer = SUSHI_ORACLE.latestAnswer * exchangeRate / 1 ether

`SUSHI_ORACLE` (`0xe572CeF69f43c2E488b33924AF04BDacE19079cf`) is itself a
plain, standard 3-phase Chainlink SUSHI/ETH proxy (`phaseId==3`, confirmed
live) -- pullable exactly like every other feed in this project.
`SUSHI.balanceOf(xSUSHI)`/`xSUSHI.totalSupply` are live contract state, not
events, but both are exactly reconstructable from ERC20 `Transfer` event
history (verified via `getsourcecode` on xSUSHI itself: it's SushiSwap's
`SushiBar` contract, plain OpenZeppelin `ERC20` with no custom
mint/burn/enter/leave events -- `enter`/`leave` use the standard
`_mint`/`_burn`, which only ever emit the standard `Transfer` event with
`from`/`to` = the zero address):

 SUSHI.balanceOf(xSUSHI) at any block = running sum of SUSHI `Transfer`
 events with `to`==xSUSHI (+amount) minus `from`==xSUSHI (-amount).
 Not just `enter`/`leave` calls -- SushiSwap's fee-distribution
 mechanism periodically sends SUSHI to the bar directly (this is how
 the share price actually grows over time), so ALL such transfers
 matter, not only ones coinciding with an xSUSHI mint/burn.
 xSUSHI.totalSupply at any block = running sum of xSUSHI's own
 `Transfer` events with `from`==0x0 (+amount, `enter`'s `_mint`)
 minus `to`==0x0 (-amount, `leave`'s `_burn`).

Pulled via Etherscan `getLogs` with `address`=the token contract and
`topic0`=the standard ERC20 `Transfer` topic0, `topic0_{1,2}_opr=and` +
`topic{1,2}`=the counterparty address (zero-padded) -- same
server-side-filtered-by-counterparty pattern as `fix_gateway_withdraw.py`,
which avoids pulling SUSHI's (or xSUSHI's) entire chain-wide transfer
history. Recursive 10,000-row bisection + per-query checkpointing, same
mechanics as every other.

Usage:
 python scripts/onchain/backfill_xsushi_underlying_events.py --dry-run
 python scripts/onchain/backfill_xsushi_underlying_events.py
Writes:
 data/raw/chainlink/chain=1/sushi_oracle_answer_updated.parquet
 data/raw/sushibar/chain=1/sushibar_events.parquet
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve.parent))
from fetch_svr_feed_events import ETHERSCAN_URL, get_logs_paginated # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve.parents[2] / "src"))
from cascadesignal.ingest.schema import normalize, to_arrow # noqa: E402

_CHECKPOINT_DIR = Path("data/raw/.checkpoints")
_ORACLE_OUT_PARQUET = Path(
 "data/raw/chainlink/chain=1/sushi_oracle_answer_updated.parquet"
)
_EVENTS_OUT_PARQUET = Path("data/raw/sushibar/chain=1/sushibar_events.parquet")

FULL_HISTORY_START_BLOCK = 10_800_000 # SushiBar deployed ~block 10,802,010

SUSHI = "0x6b3595068778dd592e39a122f4f5a5cf09c90fe2"
XSUSHI = "0x8798249c2e607446efb7ad49ec89dd1865ff4272"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# SUSHI_ORACLE's 3 real on-chain phases (phaseId==3, walked via
# phaseAggregators(1..3), verified live this session).
SUSHI_ORACLE_PHASES = [
 "0x00377d6c82df8f63163ff828760b2a5d935734cf",
 "0xd01bbb3afed2cb5ca92ca3834d441dc737f0da70",
 "0x0e6d6293b6d4801ef491bd762988cfdabc0ecb09",
]

TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_MAX_RESULTS_PER_PAGE = 1000
_ETHERSCAN_RESULT_WINDOW_CAP = 10_000
_MAX_PAGE = _ETHERSCAN_RESULT_WINDOW_CAP // _MAX_RESULTS_PER_PAGE


def _topic_address(address: str) -> str:
 return "0x" + address.lower.removeprefix("0x").rjust(64, "0")


def get_block_number -> int:
 payload: dict[str, Any] = {
 "jsonrpc": "2.0",
 "method": "eth_blockNumber",
 "params": [],
 "id": 1,
 }
 resp = requests.post(
 "https://ethereum-rpc.publicnode.com", json=payload, timeout=20
 )
 return int(resp.json["result"], 16)


def _get_transfer_logs_page(
 token_address: str,
 topic_position: int,
 counterparty: str,
 api_key: str,
 from_block: int,
 to_block: int,
) -> list[dict]:
 logs: list[dict] = []
 page = 1
 while page <= _MAX_PAGE:
 params: dict[str, Any] = {
 "chainid": 1,
 "module": "logs",
 "action": "getLogs",
 "address": token_address,
 "topic0": TRANSFER_TOPIC0,
 f"topic0_{topic_position}_opr": "and",
 f"topic{topic_position}": _topic_address(counterparty),
 "fromBlock": from_block,
 "toBlock": to_block,
 "page": page,
 "offset": _MAX_RESULTS_PER_PAGE,
 "apikey": api_key,
 }
 result = None
 for attempt in range(4):
 time.sleep(0.21)
 try:
 resp = requests.get(ETHERSCAN_URL, params=params, timeout=20)
 payload = resp.json
 except (requests.RequestException, ValueError):
 time.sleep(2.0 * (attempt + 1))
 continue
 candidate = payload.get("result")
 if isinstance(candidate, list):
 result = candidate
 break
 if payload.get("message") == "No records found":
 result = []
 break
 time.sleep(2.0 * (attempt + 1))
 if result is None:
 raise RuntimeError(
 f"Etherscan getLogs kept failing for {token_address} "
 f"topic{topic_position}={counterparty} [{from_block},{to_block}] "
 f"page {page}"
 )
 if not result:
 break
 logs.extend(result)
 if len(result) < _MAX_RESULTS_PER_PAGE:
 break
 page += 1
 return logs


def get_transfer_logs(
 token_address: str,
 topic_position: int,
 counterparty: str,
 api_key: str,
 from_block: int,
 to_block: int,
) -> list[dict]:
 """All `Transfer` logs on `token_address` with the address at
 `topic_position` (1=`from`, 2=`to`) == `counterparty`, recursively
 bisecting on the 10k-result cap."""
 logs = _get_transfer_logs_page(
 token_address, topic_position, counterparty, api_key, from_block, to_block
 )
 if len(logs) < _ETHERSCAN_RESULT_WINDOW_CAP or from_block >= to_block:
 return logs
 print(
 f" {token_address} topic{topic_position}={counterparty} "
 f"[{from_block},{to_block}] hit the 10k cap, bisecting...",
 flush=True,
 )
 mid = (from_block + to_block) // 2
 left = get_transfer_logs(
 token_address, topic_position, counterparty, api_key, from_block, mid
 )
 right = get_transfer_logs(
 token_address, topic_position, counterparty, api_key, mid + 1, to_block
 )
 return left + right


def _pull_transfer_kind(
 label: str,
 token_address: str,
 topic_position: int,
 counterparty: str,
 api_key: str,
 from_block: int,
 to_block: int,
) -> list[dict]:
 checkpoint = _CHECKPOINT_DIR / f"sushibar_events_{label}.json"
 if checkpoint.exists:
 cached: list[dict] = json.loads(checkpoint.read_text)
 print(f" {label}: {len(cached)} rows (checkpoint)", flush=True)
 return cached
 logs = get_transfer_logs(
 token_address, topic_position, counterparty, api_key, from_block, to_block
 )
 _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
 checkpoint.write_text(json.dumps(logs))
 print(f" {label}: {len(logs)} rows (checkpointed)", flush=True)
 return logs


def _get_transaction_receipt(tx_hash: str, retries: int = 4) -> dict | None:
 payload: dict[str, Any] = {
 "jsonrpc": "2.0",
 "method": "eth_getTransactionReceipt",
 "params": [tx_hash],
 "id": 1,
 }
 for attempt in range(retries):
 try:
 resp = requests.post(
 "https://ethereum-rpc.publicnode.com", json=payload, timeout=20
 )
 result = resp.json
 except requests.RequestException:
 time.sleep(1.0 * (attempt + 1))
 continue
 if "error" in result or result.get("result") is None:
 time.sleep(1.0 * (attempt + 1))
 continue
 return result["result"]
 return None


def _resolve_log_index(log: dict) -> int:
 """Etherscan's `getLogs` occasionally returns `logIndex: "0x"` (empty) for
 an otherwise-real log -- confirmed on ~0.1% of xSUSHI mint events, not
 concentrated in any one era (74 of 111 seen were after Aave's own xSUSHI
 listing block, so this isn't just an early-history artifact to shrug off).
 Falls back to `eth_getTransactionReceipt` (permanently available on any
 RPC, not archive-gated, since a receipt is fixed at inclusion time) and
 matches by (address, topics, data) to recover the real log_index rather
 than silently dropping a real balance/supply-affecting event."""
 if log["logIndex"] != "0x":
 return int(log["logIndex"], 16)
 receipt = _get_transaction_receipt(log["transactionHash"])
 if receipt is None:
 raise RuntimeError(
 f"Could not resolve logIndex for {log['transactionHash']} "
 "(receipt fetch failed)"
 )
 matches = [
 rl
 for rl in receipt["logs"]
 if rl["address"].lower == log["address"].lower
 and rl["topics"] == log["topics"]
 and rl["data"] == log["data"]
 ]
 if not matches:
 raise RuntimeError(
 f"Could not resolve logIndex for {log['transactionHash']} "
 "(no matching log in receipt)"
 )
 return int(matches[0]["logIndex"], 16)


def _event_rows(kind: str, sign: int, logs: list[dict]) -> list[dict]:
 rows = []
 for log in logs:
 amount = int(log["data"], 16)
 rows.append(
 {
 "chain_id": 1,
 "block_number": int(log["blockNumber"], 16),
 "block_timestamp": pd.Timestamp(
 int(log["timeStamp"], 16), unit="s", tz="UTC"
 ),
 "tx_hash": log["transactionHash"],
 "log_index": _resolve_log_index(log),
 "kind": kind,
 # Stored as a string, not a native int -- 18-decimal raw
 # token amounts routinely exceed int64 (e.g. any transfer
 # over ~9.2 whole tokens), same convention as `amount_raw`
 # everywhere else in this project.
 "signed_amount": str(sign * amount),
 }
 )
 return rows


def _pull_oracle_phase(
 addr: str, api_key: str, from_block: int, to_block: int
) -> list[dict]:
 checkpoint = _CHECKPOINT_DIR / f"sushi_oracle_{addr}.json"
 if checkpoint.exists:
 cached: list[dict] = json.loads(checkpoint.read_text)
 print(f" SUSHI/ETH {addr}: {len(cached)} rows (checkpoint)", flush=True)
 return cached
 logs = get_logs_paginated(addr, api_key, from_block=from_block, to_block=to_block)
 _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
 checkpoint.write_text(json.dumps(logs))
 print(f" SUSHI/ETH {addr}: {len(logs)} rows (checkpointed)", flush=True)
 return logs


def _oracle_rows_from_logs(aggregator: str, logs: list[dict]) -> list[dict]:
 rows = []
 for log in logs:
 answer = int(log["topics"][1], 16)
 if answer >= 2**255:
 answer -= 2**256
 rows.append(
 {
 "chain_id": 1,
 "block_number": int(log["blockNumber"], 16),
 "block_timestamp": pd.Timestamp(
 int(log["timeStamp"], 16), unit="s", tz="UTC"
 ),
 "tx_hash": log["transactionHash"],
 "log_index": int(log["logIndex"], 16),
 "protocol": "chainlink",
 "event_type": "AnswerUpdated",
 "user": aggregator.lower,
 "collateral_asset": None,
 "debt_asset": None,
 "amount_raw": str(answer),
 "amount_usd": answer / 1e18,
 "liquidator": None,
 "collateral_seized_raw": None,
 "collateral_seized_usd": None,
 }
 )
 return rows


def main -> None:
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument(
 "--dry-run", action="store_true", help="Print the plan, pull nothing."
 )
 args = parser.parse_args

 chain_tip = get_block_number
 print(f"blocks [{FULL_HISTORY_START_BLOCK}, {chain_tip}] (chain tip)\n")
 print("Plan: SUSHI/ETH 3 phases, SUSHI<->xSUSHI transfers, xSUSHI mint/burn\n")
 if args.dry_run:
 return

 api_key = os.environ["ETHERSCAN_API_KEY"]

 print("SUSHI/ETH oracle phases:", flush=True)
 oracle_rows: list[dict] = []
 for addr in SUSHI_ORACLE_PHASES:
 logs = _pull_oracle_phase(addr, api_key, FULL_HISTORY_START_BLOCK, chain_tip)
 oracle_rows.extend(_oracle_rows_from_logs(addr, logs))

 oracle_df = normalize(pd.DataFrame(oracle_rows), protocol="chainlink")
 oracle_df = oracle_df.drop_duplicates(subset=["user", "block_number", "log_index"])
 _ORACLE_OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
 to_arrow(oracle_df).to_pandas.to_parquet(_ORACLE_OUT_PARQUET, index=False)
 print(f"Wrote {len(oracle_df)} rows to {_ORACLE_OUT_PARQUET}\n")

 print("SushiBar balance/supply events:", flush=True)
 event_rows: list[dict] = []
 event_rows.extend(
 _event_rows(
 "sushi_balance",
 +1,
 _pull_transfer_kind(
 "sushi_to_xsushi",
 SUSHI,
 2,
 XSUSHI,
 api_key,
 FULL_HISTORY_START_BLOCK,
 chain_tip,
 ),
 )
 )
 event_rows.extend(
 _event_rows(
 "sushi_balance",
 -1,
 _pull_transfer_kind(
 "sushi_from_xsushi",
 SUSHI,
 1,
 XSUSHI,
 api_key,
 FULL_HISTORY_START_BLOCK,
 chain_tip,
 ),
 )
 )
 event_rows.extend(
 _event_rows(
 "xsushi_supply",
 +1,
 _pull_transfer_kind(
 "xsushi_mint",
 XSUSHI,
 1,
 ZERO_ADDRESS,
 api_key,
 FULL_HISTORY_START_BLOCK,
 chain_tip,
 ),
 )
 )
 event_rows.extend(
 _event_rows(
 "xsushi_supply",
 -1,
 _pull_transfer_kind(
 "xsushi_burn",
 XSUSHI,
 2,
 ZERO_ADDRESS,
 api_key,
 FULL_HISTORY_START_BLOCK,
 chain_tip,
 ),
 )
 )

 events_df = pd.DataFrame(event_rows)
 events_df = events_df.drop_duplicates(subset=["kind", "block_number", "log_index"])
 events_df = events_df.sort_values(
 ["kind", "block_number", "log_index"], kind="mergesort"
 ).reset_index(drop=True)
 _EVENTS_OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
 events_df.to_parquet(_EVENTS_OUT_PARQUET, index=False)
 print(f"Wrote {len(events_df)} rows to {_EVENTS_OUT_PARQUET}")


if __name__ == "__main__":
 main
