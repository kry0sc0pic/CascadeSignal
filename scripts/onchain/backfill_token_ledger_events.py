"""Full-history token-level ledger events for Aave v2.

The engine currently replays `LendingPool` events (Deposit/Borrow/Repay/
Withdraw/LiquidationCall) plus two heuristic corrections (Track C's naked
aToken-transfer pull, `fix_gateway_withdraw.py`'s Withdraw/Transfer
correlation), then converts each raw delta into a scaled balance by dividing
by the *nearest prior stored* `ReserveDataUpdated` index (`_scale_ledger_rows`
in `state/engine.py`) -- an approximation on two axes: the LendingPool event
stream requires per-quirk correction to attribute the right user (gateways,
periphery adapters), and the scaling index is a lookup, not the exact value
Aave itself used.

The aToken/debtToken contracts emit the *exact* accounting directly, with the
index embedded in the event itself:

 AToken: Mint(from, value, index)
 Burn(from, target, value, index)
 BalanceTransfer(from, to, value, index)
 VariableDebtToken: Mint(from, onBehalfOf, value, index)
 Burn(user, amount, index)
 StableDebtToken: Mint(user, onBehalfOf, amount, currentBalance,
 balanceIncrease, newRate, avgStableRate,
 newTotalSupply)
 Burn(user, amount, currentBalance, balanceIncrease,
 avgStableRate, newTotalSupply)

Replaying these directly (see `state/engine.py`'s H3 section) is a strict
upgrade: gateway/periphery-adapter routing is resolved *for free* (the
contract's own `from`/`onBehalfOf`/`user` fields always name the real
position holder, regardless of which wrapper called it -- superseding both
Track C's naked-transfer pull and `fix_gateway_withdraw.py`'s correlation
heuristic), the index is exact (no nearest-prior lookup error), and stable
debt gets its own compounding curve at the position's locked `avgStableRate`
instead of being incorrectly scaled by the pool's variable-borrow index.

All 7 event signatures below were derived via `Crypto.Hash.keccak`
(pycryptodome) then verified two ways against real Etherscan `getLogs`
results before use (this repo's established discipline): (1) topic0 exactly
matched an observed on-chain topic for every signature, including expected
topic *counts* (indexed-arg counts) and data *word* counts; (2) address-field
order (which topic is the real position holder, e.g. `onBehalfOf` vs the
calling wrapper) was cross-checked against the already-verified
`fix_gateway_onbehalfof.py` `Borrow.onBehalfOf` field for shared tx hashes
(5/5 matches for VariableDebtToken.Mint; AToken Mint/Burn/BalanceTransfer
field order confirmed via each event's companion standard ERC20 `Transfer`
in the same tx, whose `from`/`to` order is unambiguous). `Transfer.value` was
also confirmed to already equal the real (non-scaled) amount, matching
`BalanceTransfer.value`/`Mint.value` exactly in every cross-checked example.

Usage:
 python scripts/onchain/backfill_token_ledger_events.py --dry-run
 python scripts/onchain/backfill_token_ledger_events.py
Writes three parquets under `data/raw/corrections/aave_v2_token_ledger/chain=1/`:
 atoken_events.parquet (block_number, log_index, tx_hash, reserve,
 event_type, address_1, address_2,
 value_raw, index_raw)
 variable_debt_events.parquet (block_number, log_index, tx_hash, reserve,
 event_type, user, value_raw, index_raw)
 stable_debt_events.parquet (block_number, log_index, block_timestamp,
 tx_hash, reserve, event_type, user,
 amount_raw, current_balance_raw,
 avg_stable_rate_raw)
`state/engine.py` applies these automatically on every future load if all
three files exist (no-op-when-missing, same convention as every other).
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
from fetch_svr_feed_events import get_logs_paginated # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve.parents[2] / "src"))
from cascadesignal.state.reserves import ( # noqa: E402
 ATOKEN_ADDRESS_BY_RESERVE,
 STABLE_DEBT_TOKEN_ADDRESS_BY_RESERVE,
 VARIABLE_DEBT_TOKEN_ADDRESS_BY_RESERVE,
)

RPC_URL = "https://ethereum-rpc.publicnode.com"
_CHECKPOINT_DIR = Path("data/raw/.checkpoints")
_OUT_DIR = Path("data/raw/corrections/aave_v2_token_ledger/chain=1")

# Aave v2's actual launch block (see backfill_aave_v2_launch_events.py); the
# upper bound is resolved live against the chain tip at run time -- same
# convention as backfill_chainlink_eth_feeds_full_history.py (H2).
FULL_HISTORY_START_BLOCK = 11_362_000

# keccak256(<event signature>), derived via Crypto.Hash.keccak, verified live
# against real Etherscan getLogs results for aWETH / variableDebtWETH /
# stableDebtUSDC before use -- see module docstring.
ATOKEN_MINT_TOPIC0 = (
 "0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f"
)
ATOKEN_BURN_TOPIC0 = (
 "0x5d624aa9c148153ab3446c1b154f660ee7701e549fe9b62dab7171b1c80e6fa2"
)
ATOKEN_BALANCE_TRANSFER_TOPIC0 = (
 "0x4beccb90f994c31aced7a23b5611020728a23d8ec5cddd1a3e9d97b96fda8666"
)
VDEBT_MINT_TOPIC0 = "0x2f00e3cdd69a77be7ed215ec7b2a36784dd158f921fca79ac29deffa353fe6ee"
VDEBT_BURN_TOPIC0 = "0x49995e5dd6158cf69ad3e9777c46755a1a826a446c6416992167462dad033b2a"
SDEBT_MINT_TOPIC0 = "0xc16f4e4ca34d790de4c656c72fd015c667d688f20be64eea360618545c4c530f"
SDEBT_BURN_TOPIC0 = "0x44bd20a79e993bdcc7cbedf54a3b4d19fb78490124b6b90d04fe3242eea579e8"


def get_block_number -> int:
 payload: dict[str, Any] = {
 "jsonrpc": "2.0",
 "method": "eth_blockNumber",
 "params": [],
 "id": 1,
 }
 resp = requests.post(RPC_URL, json=payload, timeout=20)
 return int(resp.json["result"], 16)


def _pull(
 address: str,
 event_type: str,
 topic0: str,
 api_key: str,
 from_block: int,
 to_block: int,
) -> list[dict]:
 checkpoint = _CHECKPOINT_DIR / f"token_ledger_{event_type}_{address}.json"
 if checkpoint.exists:
 logs: list[dict] = json.loads(checkpoint.read_text)
 print(
 f" {event_type:16s} {address}: {len(logs)} rows (checkpoint)", flush=True
 )
 return logs

 logs = get_logs_paginated(
 address, api_key, from_block=from_block, to_block=to_block, topic0=topic0
 )
 _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
 checkpoint.write_text(json.dumps(logs))
 print(f" {event_type:16s} {address}: {len(logs)} rows (checkpointed)", flush=True)
 return logs


def _pull_with_retry(
 address: str,
 event_type: str,
 topic0: str,
 api_key: str,
 from_block: int,
 to_block: int,
 attempts: int = 4,
) -> list[dict]:
 for attempt in range(attempts):
 try:
 return _pull(address, event_type, topic0, api_key, from_block, to_block)
 except RuntimeError as exc:
 if attempt == attempts - 1:
 raise
 wait = 30 * (attempt + 1)
 print(f" retry {event_type} {address} in {wait}s after: {exc}", flush=True)
 time.sleep(wait)
 raise AssertionError("unreachable") # pragma: no cover


def _hex_to_int(value: str) -> int:
 return int(value, 16) if value not in ("0x", "", None) else 0


def _topic_addr(topic: str) -> str:
 return ("0x" + topic[-40:]).lower


def _words(data: str) -> list[int]:
 body = data[2:]
 return [int(body[i : i + 64], 16) for i in range(0, len(body), 64)]


def _decode_atoken(reserve: str, event_type: str, logs: list[dict]) -> list[dict]:
 rows = []
 for log in logs:
 w = _words(log["data"])
 row = {
 "block_number": _hex_to_int(log["blockNumber"]),
 "log_index": _hex_to_int(log["logIndex"]),
 "tx_hash": log["transactionHash"],
 "reserve": reserve,
 "event_type": event_type,
 "value_raw": str(w[0]),
 "index_raw": str(w[1]),
 }
 if event_type == "Mint":
 # Mint(address indexed from, uint256 value, uint256 index)
 # "from" here names the *credited* account (verified against its
 # companion Transfer(0x0, from, value) in the same tx).
 row["address_1"] = _topic_addr(log["topics"][1])
 row["address_2"] = None
 elif event_type == "Burn":
 # Burn(address indexed from, address indexed target, value, index)
 # -- "from" is the *debited* account; "target" (where the
 # underlying goes) is irrelevant to the scaled-balance ledger.
 row["address_1"] = _topic_addr(log["topics"][1])
 row["address_2"] = _topic_addr(log["topics"][2])
 else: # BalanceTransfer(from indexed, to indexed, value, index)
 row["address_1"] = _topic_addr(log["topics"][1])
 row["address_2"] = _topic_addr(log["topics"][2])
 rows.append(row)
 return rows


def _decode_variable_debt(
 reserve: str, event_type: str, logs: list[dict]
) -> list[dict]:
 rows = []
 for log in logs:
 w = _words(log["data"])
 if event_type == "Mint":
 # Mint(address indexed from, address indexed onBehalfOf, value,
 # index) -- onBehalfOf (topics[2]) is the real debtor, verified
 # 5/5 against fix_gateway_onbehalfof.py's Borrow.onBehalfOf for
 # shared tx hashes (see module docstring).
 user = _topic_addr(log["topics"][2])
 else: # Burn(address indexed user, uint256 amount, uint256 index)
 user = _topic_addr(log["topics"][1])
 rows.append(
 {
 "block_number": _hex_to_int(log["blockNumber"]),
 "log_index": _hex_to_int(log["logIndex"]),
 "tx_hash": log["transactionHash"],
 "reserve": reserve,
 "event_type": event_type,
 "user": user,
 "value_raw": str(w[0]),
 "index_raw": str(w[1]),
 }
 )
 return rows


def _decode_stable_debt(reserve: str, event_type: str, logs: list[dict]) -> list[dict]:
 rows = []
 for log in logs:
 w = _words(log["data"])
 if event_type == "Mint":
 # Mint(user indexed, onBehalfOf indexed, amount, currentBalance,
 # balanceIncrease, newRate, avgStableRate, newTotalSupply)
 # onBehalfOf (topics[2]) is the real debtor, same convention as
 # VariableDebtToken.Mint.
 user = _topic_addr(log["topics"][2])
 avg_stable_rate_raw = w[4]
 else: # Burn(user indexed, amount, currentBalance, balanceIncrease,
 # avgStableRate, newTotalSupply)
 user = _topic_addr(log["topics"][1])
 avg_stable_rate_raw = w[3]
 rows.append(
 {
 "block_number": _hex_to_int(log["blockNumber"]),
 "log_index": _hex_to_int(log["logIndex"]),
 "block_timestamp": pd.Timestamp(
 _hex_to_int(log["timeStamp"]), unit="s", tz="UTC"
 ),
 "tx_hash": log["transactionHash"],
 "reserve": reserve,
 "event_type": event_type,
 "user": user,
 "amount_raw": str(w[0]),
 "current_balance_raw": str(w[1]),
 "avg_stable_rate_raw": str(avg_stable_rate_raw),
 }
 )
 return rows


_ATOKEN_JOBS = [
 ("Mint", ATOKEN_MINT_TOPIC0),
 ("Burn", ATOKEN_BURN_TOPIC0),
 ("BalanceTransfer", ATOKEN_BALANCE_TRANSFER_TOPIC0),
]
_VDEBT_JOBS = [("Mint", VDEBT_MINT_TOPIC0), ("Burn", VDEBT_BURN_TOPIC0)]
_SDEBT_JOBS = [("Mint", SDEBT_MINT_TOPIC0), ("Burn", SDEBT_BURN_TOPIC0)]


def main -> None:
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument(
 "--dry-run", action="store_true", help="Print the plan, pull nothing."
 )
 args = parser.parse_args

 chain_tip = get_block_number
 total_jobs = (
 len(ATOKEN_ADDRESS_BY_RESERVE) * len(_ATOKEN_JOBS)
 + len(VARIABLE_DEBT_TOKEN_ADDRESS_BY_RESERVE) * len(_VDEBT_JOBS)
 + len(STABLE_DEBT_TOKEN_ADDRESS_BY_RESERVE) * len(_SDEBT_JOBS)
 )
 print(
 f"Plan: {len(ATOKEN_ADDRESS_BY_RESERVE)} reserves x "
 f"(3 aToken + 2 variableDebt + 2 stableDebt) = {total_jobs} pull jobs, "
 f"blocks [{FULL_HISTORY_START_BLOCK}, {chain_tip}] (chain tip)\n"
 )
 if args.dry_run:
 return

 api_key = os.environ["ETHERSCAN_API_KEY"]
 done = 0

 atoken_rows: list[dict] = []
 for reserve, addr in sorted(ATOKEN_ADDRESS_BY_RESERVE.items):
 for event_type, topic0 in _ATOKEN_JOBS:
 done += 1
 print(f"[{done}/{total_jobs}]", end=" ", flush=True)
 logs = _pull_with_retry(
 addr,
 f"atoken_{event_type}",
 topic0,
 api_key,
 FULL_HISTORY_START_BLOCK,
 chain_tip,
 )
 atoken_rows.extend(_decode_atoken(reserve, event_type, logs))

 vdebt_rows: list[dict] = []
 for reserve, addr in sorted(VARIABLE_DEBT_TOKEN_ADDRESS_BY_RESERVE.items):
 for event_type, topic0 in _VDEBT_JOBS:
 done += 1
 print(f"[{done}/{total_jobs}]", end=" ", flush=True)
 logs = _pull_with_retry(
 addr,
 f"vdebt_{event_type}",
 topic0,
 api_key,
 FULL_HISTORY_START_BLOCK,
 chain_tip,
 )
 vdebt_rows.extend(_decode_variable_debt(reserve, event_type, logs))

 sdebt_rows: list[dict] = []
 for reserve, addr in sorted(STABLE_DEBT_TOKEN_ADDRESS_BY_RESERVE.items):
 for event_type, topic0 in _SDEBT_JOBS:
 done += 1
 print(f"[{done}/{total_jobs}]", end=" ", flush=True)
 logs = _pull_with_retry(
 addr,
 f"sdebt_{event_type}",
 topic0,
 api_key,
 FULL_HISTORY_START_BLOCK,
 chain_tip,
 )
 sdebt_rows.extend(_decode_stable_debt(reserve, event_type, logs))

 _OUT_DIR.mkdir(parents=True, exist_ok=True)

 if atoken_rows:
 df = pd.DataFrame(atoken_rows).drop_duplicates(subset=["tx_hash", "log_index"])
 df = df.sort_values(
 ["block_number", "log_index"], kind="mergesort"
 ).reset_index(drop=True)
 df.to_parquet(_OUT_DIR / "atoken_events.parquet", index=False)
 print(f"\nWrote {len(df)} aToken event rows")

 if vdebt_rows:
 df = pd.DataFrame(vdebt_rows).drop_duplicates(subset=["tx_hash", "log_index"])
 df = df.sort_values(
 ["block_number", "log_index"], kind="mergesort"
 ).reset_index(drop=True)
 df.to_parquet(_OUT_DIR / "variable_debt_events.parquet", index=False)
 print(f"Wrote {len(df)} variableDebtToken event rows")

 if sdebt_rows:
 df = pd.DataFrame(sdebt_rows).drop_duplicates(subset=["tx_hash", "log_index"])
 df = df.sort_values(
 ["block_number", "log_index"], kind="mergesort"
 ).reset_index(drop=True)
 df.to_parquet(_OUT_DIR / "stable_debt_events.parquet", index=False)
 print(f"Wrote {len(df)} stableDebtToken event rows")


if __name__ == "__main__":
 main
