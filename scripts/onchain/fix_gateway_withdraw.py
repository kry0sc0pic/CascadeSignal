"""Fix Aave v2 `Withdraw` gateway misattribution via aToken `Transfer` correlation.

Companion fix to `fix_gateway_onbehalfof.py`. That script showed
`Deposit`/`Borrow` carry a decodable `onBehalfOf` field, but `Withdraw`
doesn't: a gateway-routed withdrawal (e.g. `WETHGateway.withdrawETH`) is a
two-step on-chain flow
 1. `aToken.transferFrom(realUser, gateway, amount)` -- the gateway pulls
 the real user's aTokens into itself (`LendingPool.withdraw` can only
 burn from its own caller's balance, so the gateway must hold them
 first). This emits an ERC20 `Transfer(realUser, gateway, amount)` on
 the *aToken* contract.
 2. `LendingPool.withdraw(asset, amount, to)` called *by the gateway*
 `msg.sender` here is the gateway, not the real user, so the emitted
 `Withdraw` event's `user` field (Aave v2's ABI: `user` = msg.sender)
 names the gateway.

Verified directly against a real receipt (tx
0x9e80324d...901917e2, block 12,024,573): the aWETH `Transfer` log
(`from`=`0xb5c1d8e1...`, `to`=WETHGateway, log_index 46) precedes the
`Withdraw` log (`user`=WETHGateway, log_index 54) in the same tx, and its
amount (3000481446631344445) matches the `Withdraw`'s `amount_raw` exactly.
Direct (non-gateway) withdrawals have no such preceding aToken `Transfer`
*to* their own `user` value, so the correlation is a no-op for them, as
required.

Pull strategy -- **filter by (aToken address, recipient `topic2`) pairs, not
by recipient alone**: a first attempt filtered chain-wide by `topic2` =
gateway address only (no `address` filter), reasoning that a
periphery/adapter contract's whole purpose is to shuttle funds through, not
accumulate them, so "tokens received by this one contract" should be small.
That held for the long tail but broke down for the top gateway
(WETHGateway, `0xcc9a0b7c...`): its inbound `Transfer` count kept hitting
Etherscan's 10k-result cap even after bisecting to ~400k-block windows
across the *entire* 13M-block range -- consistent with dusting-attack spam
tokens (a well-documented pattern of unsolicited airdrops to
high-activity/popular addresses), not real Aave activity. Restricting each
query to a **specific (aToken address, gateway) pair**
`address`=the aToken contract *and* `topic0_2_opr=and`, `topic2`=the
gateway -- filters out that noise server-side instead of pulling and
discarding it locally, and only real aToken-to-gateway transfers can ever
match both conditions. The (gateway, reserve) pairs actually needed are
read directly off the local `Withdraw` population (`debt_asset` is which
reserve; only gateways/reserves that co-occur in a real candidate row are
queried), which is a small fraction of the full 226 x 37 cross product.

Checkpointed per-(gateway, reserve)-pair under `data/raw/.checkpoints/`
(one `get_logs_paginated` call per pair handles the 10k-result-window
bisection internally, though in practice a correctly-scoped pair almost
never approaches that cap) -- resumable if interrupted.

Correlation: for each local `Withdraw` event whose raw `user` is a known
gateway address, look up the aToken address for its reserve
(`ATOKEN_ADDRESS_BY_RESERVE`), then find the aToken `Transfer`(s) in the
same tx with `to` == that gateway and `log_index` strictly less than the
`Withdraw`'s -- i.e. logged *before* it, as the on-chain flow requires. The
closest preceding one's `from` is the real position holder. No match (the
direct-withdrawal case) leaves the row uncorrected.

Usage:
 python scripts/onchain/fix_gateway_withdraw.py
Writes `data/raw/corrections/aave_v2_withdraw/chain=1/withdraw.parquet`
(tx_hash, log_index, real_user) -- `cascadesignal.state.engine.load_events`
applies it (together with the existing Deposit `onBehalfOf` correction
the two must land together, see that module's docstring for why applying
Deposit alone regresses the T2 mismatch rate to 73%).
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve.parent))
sys.path.insert(0, str(Path(__file__).resolve.parent.parent.parent / "src"))

from fetch_svr_feed_events import ETHERSCAN_URL # noqa: E402

from cascadesignal.state.reserves import ATOKEN_ADDRESS_BY_RESERVE # noqa: E402

import requests # noqa: E402

# keccak256("Transfer(address,address,uint256)") -- the standard ERC20
# event, verified equal to the well-known public topic0 for this signature.
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

_MIN_BLOCK = 11_500_000
_MAX_BLOCK = 24_500_000
_MAX_RESULTS_PER_PAGE = 1000
_ETHERSCAN_RESULT_WINDOW_CAP = 10_000
_MAX_PAGE = _ETHERSCAN_RESULT_WINDOW_CAP // _MAX_RESULTS_PER_PAGE

_CHECKPOINT_DIR = Path("data/raw/.checkpoints")
_OUT_PARQUET = Path("data/raw/corrections/aave_v2_withdraw/chain=1/withdraw.parquet")

_ATOKEN_ADDRESSES = frozenset(ATOKEN_ADDRESS_BY_RESERVE.values)


def _topic_address(address: str) -> str:
 return "0x" + address.lower.removeprefix("0x").rjust(64, "0")


def _etherscan_get_logs_atoken_to(
 atoken_address: str,
 to_address: str,
 api_key: str,
 from_block: int,
 to_block: int,
 page: int,
 retries: int = 4,
) -> list[dict] | None:
 params: dict[str, Any] = {
 "chainid": 1,
 "module": "logs",
 "action": "getLogs",
 "address": atoken_address,
 "topic0": TRANSFER_TOPIC0,
 "topic0_2_opr": "and",
 "topic2": _topic_address(to_address),
 "fromBlock": from_block,
 "toBlock": to_block,
 "page": page,
 "offset": _MAX_RESULTS_PER_PAGE,
 "apikey": api_key,
 }
 for attempt in range(retries):
 time.sleep(0.21) # stay under the free-tier ~5 req/sec cap
 try:
 resp = requests.get(ETHERSCAN_URL, params=params, timeout=20)
 payload = resp.json
 except (requests.RequestException, ValueError):
 time.sleep(2.0 * (attempt + 1))
 continue
 result = payload.get("result")
 if isinstance(result, list):
 return result
 if payload.get("message") == "No records found":
 return []
 time.sleep(2.0 * (attempt + 1))
 return None


def _get_logs_page(
 atoken_address: str, to_address: str, api_key: str, from_block: int, to_block: int
) -> list[dict]:
 logs: list[dict] = []
 page = 1
 while page <= _MAX_PAGE:
 result = _etherscan_get_logs_atoken_to(
 atoken_address, to_address, api_key, from_block, to_block, page
 )
 if result is None:
 raise RuntimeError(
 f"Etherscan getLogs kept failing for atoken={atoken_address} "
 f"to={to_address} [{from_block},{to_block}] page {page}"
 )
 if not result:
 break
 logs.extend(result)
 if len(result) < _MAX_RESULTS_PER_PAGE:
 break
 page += 1
 time.sleep(0.25)
 return logs


def get_atoken_transfers_to(
 atoken_address: str,
 to_address: str,
 api_key: str,
 from_block: int = _MIN_BLOCK,
 to_block: int = _MAX_BLOCK,
) -> list[dict]:
 """All `Transfer` logs on `atoken_address` with `to` == `to_address` in
 [from_block, to_block], recursively bisecting on the 10k-result cap
 (in practice a correctly-scoped (aToken, gateway) pair essentially never
 approaches it -- see module docstring)."""
 logs = _get_logs_page(atoken_address, to_address, api_key, from_block, to_block)
 if len(logs) < _ETHERSCAN_RESULT_WINDOW_CAP or from_block >= to_block:
 return logs
 print(
 f" {atoken_address} -> {to_address} [{from_block},{to_block}] "
 "hit the 10k cap, bisecting...",
 flush=True,
 )
 mid = (from_block + to_block) // 2
 left = get_atoken_transfers_to(atoken_address, to_address, api_key, from_block, mid)
 right = get_atoken_transfers_to(
 atoken_address, to_address, api_key, mid + 1, to_block
 )
 return left + right


def _pull_pair(atoken_address: str, gateway: str, api_key: str) -> list[dict]:
 checkpoint = _CHECKPOINT_DIR / f"withdraw_transfers_{atoken_address}_{gateway}.json"
 if checkpoint.exists:
 logs: list[dict] = json.loads(checkpoint.read_text)
 return logs

 logs = get_atoken_transfers_to(atoken_address, gateway, api_key)
 _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
 checkpoint.write_text(json.dumps(logs))
 return logs


def _pull_pair_with_retry(
 atoken_address: str, gateway: str, api_key: str, attempts: int = 4
) -> list[dict]:
 for attempt in range(attempts):
 try:
 return _pull_pair(atoken_address, gateway, api_key)
 except RuntimeError as exc:
 if attempt == attempts - 1:
 raise
 wait = 30 * (attempt + 1)
 print(
 f" retry {atoken_address}/{gateway} in {wait}s after: {exc}",
 flush=True,
 )
 time.sleep(wait)
 raise AssertionError("unreachable") # pragma: no cover


def needed_pairs(withdraws: pd.DataFrame) -> list[tuple[str, str]]:
 """(aToken address, gateway) pairs actually needed -- read directly off
 the local candidate `Withdraw` population instead of the full 226 x 37
 cross product (most gateways never touch most reserves)."""
 w = withdraws.copy
 w["atoken_address"] = w["reserve"].str.lower.map(ATOKEN_ADDRESS_BY_RESERVE)
 w = w.dropna(subset=["atoken_address"])
 pairs = w[["atoken_address", "user"]].drop_duplicates
 return sorted(pairs.itertuples(index=False, name=None))


def gateway_addresses(
 events: pd.DataFrame, onbehalfof_corrections: pd.DataFrame
) -> list[str]:
 """Every address that shows up as the raw `user` on a Deposit whose
 decoded `onBehalfOf` differs -- i.e. every observed gateway/adapter
 contract, not a hardcoded allowlist (same rationale as
 `fix_gateway_onbehalfof.py`'s universal decode)."""
 dep = events[events["event_type"] == "Deposit"][["tx_hash", "log_index", "user"]]
 dep_corr = onbehalfof_corrections[
 onbehalfof_corrections["event_type"] == "Deposit"
 ][["tx_hash", "log_index", "onbehalfof"]]
 merged = dep.merge(dep_corr, on=["tx_hash", "log_index"], how="inner")
 mismatched = merged[merged["user"] != merged["onbehalfof"]]
 return sorted(mismatched["user"].unique.tolist)


def build_withdraw_corrections(
 withdraws: pd.DataFrame, transfer_rows: list[dict]
) -> pd.DataFrame:
 """Correlate each gateway-attributed `Withdraw` with the aToken
 `Transfer` that funded it in the same tx (see module docstring).
 `withdraws` needs columns tx_hash, log_index, user, reserve (the
 withdrawn asset -- `debt_asset` in the raw schema, see engine.py's
 "schema quirk" note), and amount_raw.

 Matching on (tx_hash, aToken address, `to`==gateway, log_index-before)
 alone is not sufficient: a "closest preceding transfer" heuristic
 mismatches in complex multi-step transactions (leverage-loop /
 aggregator contracts that batch several unrelated aToken movements for
 the same gateway in one tx) -- confirmed empirically: a first version
 without amount matching regressed the T2 mismatch rate from 28.1% to
 69.1% by wrongly reattributing unrelated transfers, creating phantom
 negative collateral/debt for the mismatched accounts. Requiring the
 transfer's `value` to exactly equal the `Withdraw`'s `amount_raw` (both
 represent the same underlying quantity moved in the same operation, so
 they match exactly -- verified against the manually-checked example tx
 in the module docstring) makes a false-positive match astronomically
 unlikely without that requirement doing any work a real match wouldn't
 already satisfy."""
 if not transfer_rows:
 return pd.DataFrame(columns=["tx_hash", "log_index", "real_user"])

 # Raw token amounts routinely exceed int64 (e.g. an 18-decimal token
 # amount in the 1e19+ range), so amounts are compared as canonical
 # decimal strings (via Python's arbitrary-precision `int`), not as a
 # numeric pandas dtype -- casting to int64/Int64 would silently
 # overflow or raise for exactly the large-balance transfers this
 # correlation cares most about.
 transfers = pd.DataFrame(
 {
 "tx_hash": [r["transactionHash"].lower for r in transfer_rows],
 "address": [r["address"].lower for r in transfer_rows],
 "log_index": [int(r["logIndex"], 16) for r in transfer_rows],
 "from_addr": ["0x" + r["topics"][1][-40:] for r in transfer_rows],
 "amount": [str(int(r["data"], 16)) for r in transfer_rows],
 }
 )
 transfers = transfers[transfers["address"].isin(_ATOKEN_ADDRESSES)]
 if transfers.empty:
 return pd.DataFrame(columns=["tx_hash", "log_index", "real_user"])

 w = withdraws.copy
 w["atoken_address"] = w["reserve"].str.lower.map(ATOKEN_ADDRESS_BY_RESERVE)
 w = w.dropna(subset=["atoken_address"])
 w["amount"] = w["amount_raw"].map(lambda x: str(int(x)))

 merged = w.merge(
 transfers,
 left_on=["tx_hash", "atoken_address", "amount"],
 right_on=["tx_hash", "address", "amount"],
 how="inner",
 suffixes=("_withdraw", "_transfer"),
 )
 merged = merged[merged["log_index_transfer"] < merged["log_index_withdraw"]]
 if merged.empty:
 return pd.DataFrame(columns=["tx_hash", "log_index", "real_user"])

 # Closest preceding amount-matched transfer per (tx_hash, withdraw log_index).
 merged = merged.sort_values("log_index_transfer")
 best = merged.groupby(["tx_hash", "log_index_withdraw"], as_index=False).last
 return best.rename(
 columns={"log_index_withdraw": "log_index", "from_addr": "real_user"}
 )[["tx_hash", "log_index", "real_user"]]


def _load_local_events -> pd.DataFrame:
 paths = sorted(glob.glob("data/raw/aave_v2/chain=*/*.parquet"))
 df = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
 df = df[df["event_type"].isin({"Deposit", "Withdraw"})].copy
 return df.drop_duplicates(subset=["tx_hash", "log_index"])


def main -> None:
 api_key = os.environ["ETHERSCAN_API_KEY"]

 events = _load_local_events
 onbehalfof_corrections = pd.read_parquet(
 "data/raw/corrections/aave_v2_onbehalfof/chain=1/onbehalfof.parquet"
 )
 gateways = gateway_addresses(events, onbehalfof_corrections)

 withdraws = events[events["event_type"] == "Withdraw"][
 ["tx_hash", "log_index", "user", "debt_asset", "amount_raw"]
 ].rename(columns={"debt_asset": "reserve"})
 withdraws = withdraws[withdraws["user"].isin(gateways)]

 pairs = needed_pairs(withdraws)
 print(
 f"Pulling aToken-recipient transfers for {len(pairs)} "
 f"(aToken, gateway) pairs (of {len(gateways)} gateways)...",
 flush=True,
 )

 all_transfer_rows: list[dict] = []
 for i, (atoken_address, gateway) in enumerate(pairs):
 rows = _pull_pair_with_retry(atoken_address, gateway, api_key)
 all_transfer_rows.extend(rows)
 if rows:
 print(
 f" [{i + 1}/{len(pairs)}] {atoken_address}/{gateway}: {len(rows)} rows",
 flush=True,
 )

 corrections = build_withdraw_corrections(withdraws, all_transfer_rows)
 if corrections.empty:
 raise RuntimeError("No Withdraw corrections built -- nothing to write")

 corrections["log_index"] = corrections["log_index"].astype("int32")
 _OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
 corrections.to_parquet(_OUT_PARQUET, index=False)
 print(
 f"\nWrote {len(corrections)} Withdraw correction rows to {_OUT_PARQUET} "
 f"(of {len(withdraws)} gateway-attributed Withdraw candidates)"
 )


if __name__ == "__main__":
 main
