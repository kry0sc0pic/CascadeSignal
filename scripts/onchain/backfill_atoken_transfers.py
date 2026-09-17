"""Pull Aave v2 aToken `Transfer` events, every reserve (Track C).

Track B (`backfill_collateral_toggle.py`, the collateral-eligibility toggle)
did not explain the T2 gate's dominant `unexplained` mismatch bucket (8,657 ->
8,696 events, essentially flat -- see `state/engine.py`'s module docstring).
Hand-verifying a stratified 28-liquidation sample of that bucket directly
against Etherscan (per fallback plan) found the real cause instead:
this engine's ledger (`state/engine.py`) only replays `LendingPool` events
(Deposit/Borrow/Repay/Withdraw/LiquidationCall) -- but Aave v2's aTokens are
themselves ordinary transferable ERC20s, and several of Aave's own periphery
adapters (`UniswapRepayAdapter`, `UniswapLiquiditySwapAdapter`,
`ParaSwapLiquiditySwapAdapter`) execute a repay-with-collateral or
collateral-swap by pulling a user's aTokens directly via `transferFrom`
against the aToken contract, never calling `LendingPool.withdraw` at all.
That collateral genuinely leaves the account on-chain but is invisible to
this engine's ledger -- it stays counted, making reconstructed HF look
healthier than reality (exactly the `unexplained` bucket's symptom). 19/28
(68%) of the hand-verified sample had at least one such transfer before
their liquidation trigger, vs. Track B's near-zero effect.

Standard ERC20 event, both fields indexed, no `data` payload:

 event Transfer(address indexed from, address indexed to, uint256 value);

`topic0` (`keccak256("Transfer(address,address,uint256)")`) was derived via
`Crypto.Hash.keccak` (pycryptodome) then verified against a real Etherscan
`getLogs` result for aWETH before use, same discipline as this ticket's other
pull scripts. A mint (`from == 0x0`, funding a `Deposit`) or burn (`to ==
0x0`, funding a `Withdraw`) is already captured by those `LendingPool`
events -- only "naked" transfers (both legs non-zero) represent collateral
movement this engine doesn't otherwise see, so this script keeps only those.

One further wrinkle, *not* handled here but by `state.engine` at apply time:
when a liquidator opts to `receiveAToken` instead of the underlying, Aave v2
seizes collateral via the *same* transfer mechanism -- `aToken.transferFrom
(borrower, liquidator, amount)` -- inside the `LiquidationCall` transaction
itself. That seizure is already accounted for by `build_ledger`'s
`LiquidationCall` handling, so blindly applying every naked transfer here
would double-debit the borrower for their own liquidation. Confirmed on the
hand-verified sample: 12/54 naked transfers shared a tx_hash with their own
trigger liquidation. `state.engine._atoken_transfer_ledger_rows` excludes any
transfer whose `tx_hash` also carries a `LiquidationCall` -- this script
doesn't have that context (no `events` table loaded), so it pulls
unconditionally and leaves the exclusion to the engine.

Pull mechanics: one `get_logs_paginated` call per aToken (37 total,
`reserves.ATOKEN_ADDRESS_BY_RESERVE`), Etherscan v2 `getLogs`, checkpointed
per-aToken under `data/raw/.checkpoints/` (same convention as
`fetch_svr_feed_events.py`'s per-feed checkpointing) -- busy aTokens
(aWETH/aUSDC/aDAI) recursively bisect on the 10k-row window cap, quiet ones
resolve in a single call.

Usage:
 python scripts/onchain/backfill_atoken_transfers.py
Writes `data/raw/corrections/aave_v2_atoken_transfer/chain=1/atoken_transfer.parquet`
(block_number, log_index, tx_hash, reserve, from_user, to_user, value_raw)
`cascadesignal.state.engine.PositionStateEngine` applies it automatically on
every future load if the file exists.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve.parent))
sys.path.insert(0, str(Path(__file__).resolve.parent.parent.parent / "src"))

from fetch_svr_feed_events import get_logs_paginated # noqa: E402

from cascadesignal.state.reserves import ATOKEN_ADDRESS_BY_RESERVE # noqa: E402

# keccak256("Transfer(address,address,uint256)") -- verified live against
# aWETH's real Etherscan getLogs results before use (see module docstring).
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

_MIN_BLOCK = 11_500_000
_MAX_BLOCK = 24_500_000
_ZERO = "0x0000000000000000000000000000000000000000"

_CHECKPOINT_DIR = Path("data/raw/.checkpoints")
_OUT_PARQUET = Path(
 "data/raw/corrections/aave_v2_atoken_transfer/chain=1/atoken_transfer.parquet"
)


def _hex_to_int(value: str) -> int:
 """Same bare-`"0x"` zero-encoding quirk `backfill_collateral_toggle.py`
 found for `logIndex` -- normalize defensively here too."""
 return int(value, 16) if value not in ("0x", "", None) else 0


def _pull_atoken(reserve: str, atoken: str, api_key: str) -> list[dict]:
 checkpoint = _CHECKPOINT_DIR / f"atoken_transfer_{atoken}.json"
 if checkpoint.exists:
 logs: list[dict] = json.loads(checkpoint.read_text)
 print(f" {reserve} ({atoken}): {len(logs)} rows (checkpoint)", flush=True)
 return logs

 logs = get_logs_paginated(
 atoken,
 api_key,
 from_block=_MIN_BLOCK,
 to_block=_MAX_BLOCK,
 topic0=TRANSFER_TOPIC0,
 )
 _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
 checkpoint.write_text(json.dumps(logs))
 print(f" {reserve} ({atoken}): {len(logs)} rows (checkpointed)", flush=True)
 return logs


def _pull_atoken_with_retry(
 reserve: str, atoken: str, api_key: str, attempts: int = 4
) -> list[dict]:
 for attempt in range(attempts):
 try:
 return _pull_atoken(reserve, atoken, api_key)
 except RuntimeError as exc:
 if attempt == attempts - 1:
 raise
 wait = 30 * (attempt + 1)
 print(f" retry {reserve} ({atoken}) in {wait}s after: {exc}", flush=True)
 time.sleep(wait)
 raise AssertionError("unreachable") # pragma: no cover


def _decode(reserve: str, logs: list[dict]) -> list[dict]:
 rows = []
 for log in logs:
 from_addr = ("0x" + log["topics"][1][-40:]).lower
 to_addr = ("0x" + log["topics"][2][-40:]).lower
 if from_addr == _ZERO or to_addr == _ZERO:
 continue # mint/burn -- already captured by Deposit/Withdraw
 rows.append(
 {
 "block_number": _hex_to_int(log["blockNumber"]),
 "log_index": _hex_to_int(log["logIndex"]),
 "tx_hash": log["transactionHash"],
 "reserve": reserve,
 "from_user": from_addr,
 "to_user": to_addr,
 "value_raw": str(_hex_to_int(log["data"])),
 }
 )
 return rows


def main -> None:
 api_key = os.environ["ETHERSCAN_API_KEY"]
 print(
 f"Pulling aToken Transfer events over {len(ATOKEN_ADDRESS_BY_RESERVE)} "
 f"reserves [{_MIN_BLOCK}, {_MAX_BLOCK}]...",
 flush=True,
 )

 all_rows: list[dict] = []
 for reserve, atoken in sorted(ATOKEN_ADDRESS_BY_RESERVE.items):
 logs = _pull_atoken_with_retry(reserve, atoken, api_key)
 all_rows.extend(_decode(reserve, logs))

 if not all_rows:
 raise RuntimeError("No aToken Transfer logs pulled -- nothing to write")

 df = pd.DataFrame(all_rows).drop_duplicates(subset=["tx_hash", "log_index"])
 df["block_number"] = df["block_number"].astype("int64")
 df["log_index"] = df["log_index"].astype("int32")
 df = df.sort_values(["block_number", "log_index"], kind="mergesort").reset_index(
 drop=True
 )
 _OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
 df.to_parquet(_OUT_PARQUET, index=False)
 print(f"\nWrote {len(df)} naked aToken-transfer rows to {_OUT_PARQUET}")


if __name__ == "__main__":
 main
