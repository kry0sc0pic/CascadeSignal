"""Pull Aave v2 per-reserve collateral-toggle events (Track B).

Investigated as Track B's leading candidate for the T2 gate's dominant
`unexplained` mismatch bucket (8,657 events, Chainlink-confirmed HF>=1 at
trigger -- see the ticket's 2026-07-19 next-steps plan).
`state.engine`'s docstring has flagged this simplification since it was
written: every `Deposit` is currently treated as collateral-eligible
unconditionally, but Aave v2 lets a user hold a reserve balance *without* it
counting toward their health factor -- either via a direct
`LendingPool.setUserUseReserveAsCollateral(reserve, false)` call, or
implicitly whenever a reserve balance is fully withdrawn (auto-re-enabled on
the next fresh deposit). Both paths emit one of:

 event ReserveUsedAsCollateralEnabled(address indexed reserve, address indexed user);
 event ReserveUsedAsCollateralDisabled(address indexed reserve, address indexed user);

directly on the `LendingPool` contract (`0x7d2768de...`, the same contract
`fix_gateway_onbehalfof.py` pulls `Deposit`/`Borrow` from). Both fields are
indexed and there's no `data` payload, so each log fully decodes from its
topics alone -- no amount-matching/correlation step needed (unlike the
`Withdraw` gateway fix). `topic0` hashes below were computed via
`Crypto.Hash.keccak` (pycryptodome) over the event signatures, then verified
against real Etherscan `getLogs` results before this pull (both topic0s
returned real rows over the LendingPool contract for [11_500_000,
11_700_000], with topics[1]/topics[2] decoding to a real WETH reserve
address and a real user address respectively) -- not memorized/guessed.

If a user's overcounted collateral (a deposited-but-disabled reserve weighted
into HF unconditionally) is what's making correctly-priced, correctly-
attributed accounts reconstruct to HF>=1 when they were really liquidated,
this pull -- applied as a per-(user, reserve) time-varying flag by
`state.engine.PositionStateEngine` at query time (not baked into the ledger,
since eligibility is a live status, not a balance delta) -- should move
those accounts back toward HF<1 at trigger.

Pull mechanics: identical chunked single-contract `getLogs` pattern to
`fix_gateway_onbehalfof.py` (Etherscan v2 `getLogs`, 250k-block windows,
`get_logs_paginated`'s 10k-result-window bisection, checkpointed under
`data/raw/.checkpoints/` for resumable progress).

Usage:
 python scripts/onchain/backfill_collateral_toggle.py
Writes `data/raw/corrections/aave_v2_collateral_toggle/chain=1/collateral_toggle.parquet`
(user, reserve, block_number, log_index, enabled)
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

from fetch_svr_feed_events import get_logs_paginated # noqa: E402

LENDING_POOL = "0x7d2768de32b0b80b7a3454c06bdac94a69ddc7a9"

# keccak256("ReserveUsedAsCollateralEnabled(address,address)")
ENABLED_TOPIC0 = "0x00058a56ea94653cdf4f152d227ace22d4c00ad99e2a43f58cb7d9e3feb295f2"
# keccak256("ReserveUsedAsCollateralDisabled(address,address)")
DISABLED_TOPIC0 = "0x44c58d81365b66dd4b1a7f36c25aa97b8c71c361ee4937adc1a00000227db5dd"
# Both verified directly against real Etherscan getLogs results -- see module
# docstring.

_TOPIC0_BY_ENABLED = {True: ENABLED_TOPIC0, False: DISABLED_TOPIC0}

# Matches fix_gateway_onbehalfof.py's padding -- local data spans block
# 11,565,036 - 23,528,875.
_MIN_BLOCK = 11_500_000
_MAX_BLOCK = 24_500_000
_CHUNK_SIZE = 250_000

_CHECKPOINT_DIR = Path("data/raw/.checkpoints")
_OUT_PARQUET = Path(
 "data/raw/corrections/aave_v2_collateral_toggle/chain=1/collateral_toggle.parquet"
)


def _chunks -> list[tuple[int, int]]:
 bounds = list(range(_MIN_BLOCK, _MAX_BLOCK, _CHUNK_SIZE)) + [_MAX_BLOCK]
 return [(lo, hi - 1) for lo, hi in zip(bounds[:-1], bounds[1:])]


def _hex_to_int(value: str) -> int:
 """Etherscan's `getLogs` occasionally returns a bare `"0x"` instead of
 `"0x0"` for a genuinely-zero field (observed on `logIndex` for the very
 first log of a tx) -- `int(value, 16)` raises on that, so normalize it
 to zero explicitly rather than letting a real (zero-valued) row crash
 the pull."""
 return int(value, 16) if value not in ("0x", "", None) else 0


def _decode(enabled: bool, logs: list[dict]) -> list[dict]:
 rows = []
 for log in logs:
 rows.append(
 {
 "block_number": _hex_to_int(log["blockNumber"]),
 "log_index": _hex_to_int(log["logIndex"]),
 "reserve": ("0x" + log["topics"][1][-40:]).lower,
 "user": ("0x" + log["topics"][2][-40:]).lower,
 "enabled": enabled,
 }
 )
 return rows


def _pull_chunk(enabled: bool, lo: int, hi: int, api_key: str) -> list[dict]:
 tag = "enabled" if enabled else "disabled"
 checkpoint = _CHECKPOINT_DIR / f"collateral_toggle_{tag}_{lo}_{hi}.json"
 if checkpoint.exists:
 rows: list[dict] = json.loads(checkpoint.read_text)
 print(f" {tag} [{lo},{hi}]: {len(rows)} rows (checkpoint)", flush=True)
 return rows

 logs = get_logs_paginated(
 LENDING_POOL,
 api_key,
 from_block=lo,
 to_block=hi,
 topic0=_TOPIC0_BY_ENABLED[enabled],
 )
 rows = _decode(enabled, logs)
 _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
 checkpoint.write_text(json.dumps(rows))
 print(f" {tag} [{lo},{hi}]: {len(rows)} rows (checkpointed)", flush=True)
 return rows


def _pull_chunk_with_retry(
 enabled: bool, lo: int, hi: int, api_key: str, attempts: int = 4
) -> list[dict]:
 for attempt in range(attempts):
 try:
 return _pull_chunk(enabled, lo, hi, api_key)
 except RuntimeError as exc:
 if attempt == attempts - 1:
 raise
 wait = 30 * (attempt + 1)
 tag = "enabled" if enabled else "disabled"
 print(f" retry {tag} [{lo},{hi}] in {wait}s after: {exc}", flush=True)
 time.sleep(wait)
 raise AssertionError("unreachable") # pragma: no cover


def main -> None:
 api_key = os.environ["ETHERSCAN_API_KEY"]
 chunks = _chunks
 print(
 f"Pulling ReserveUsedAsCollateralEnabled/Disabled over {len(chunks)} "
 f"block chunks [{_MIN_BLOCK}, {_MAX_BLOCK}]...",
 flush=True,
 )

 all_rows: list[dict] = []
 for enabled in (True, False):
 for lo, hi in chunks:
 all_rows.extend(_pull_chunk_with_retry(enabled, lo, hi, api_key))

 if not all_rows:
 raise RuntimeError("No collateral-toggle logs pulled -- nothing to write")

 df = pd.DataFrame(all_rows).drop_duplicates(subset=["block_number", "log_index"])
 df["block_number"] = df["block_number"].astype("int64")
 df["log_index"] = df["log_index"].astype("int32")
 df = df.sort_values(
 ["user", "reserve", "block_number", "log_index"], kind="mergesort"
 ).reset_index(drop=True)
 _OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
 df.to_parquet(_OUT_PARQUET, index=False)
 print(f"\nWrote {len(df)} collateral-toggle rows to {_OUT_PARQUET}")


if __name__ == "__main__":
 main
