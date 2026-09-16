"""Pull Aave v2's full-history `ReserveDataUpdated` (interest-index) log via
Etherscan, not Dune (CAS-28, Track D).

Track C (aToken-transfer correction) cut the T2 gate's `unexplained` bucket
from 8,696 to 7,160 but didn't close it. Sizing the residual population
found it's a *close-margin, systematic* gap (median HF 1.31, 72.4% within
HF<=1.5), not a few wild outliers -- pointing back at interest-index
accrual, which `state.engine`'s docstring already flags as unmodeled
("Balances are a raw token-unit replay ... do NOT include aToken/debt-token
interest accrual between events"). The existing accrual estimate
(`scripts/analysis/estimate_interest_accrual_gap.py`, mean 0.15%, max 1.01%)
measured accrual only from the *start of each golden-episode's 300k-block
pulled window* to the trigger, not from the account's actual `Borrow`
event -- its own docstring admits this is a lower bound, and hand-tracing a
"typical" unexplained case found the account's real `Borrow` predates the
episode window by months, so most of its true holding period was never
measured.

`InterestIndexOracle` (`state/interest_index.py`) is currently backed by
`scripts/dune/aave_v2_reserve_data_updated.sql` via Dune -- but the
project's Dune budget (~487 credits left on the usable key, per the
API Keys & Credit Budget Notion page) can't cover this: a single 500k-block
chunk of this same *comparably-sized* core-event volume previously cost
~1,058 credits (the "cost lesson" on that page), and a full 5.5-year pull
was estimated at 7,000-27,500 credits. `ReserveDataUpdated` is emitted by
the same `LendingPool` contract this ticket has pulled Deposit/Borrow/Repay/
Withdraw/LiquidationCall/ReserveUsedAsCollateralEnabled/Disabled from all
along via Etherscan (free, rate-limited only, not credit-metered, and
already proven at comparable-or-larger volume: 775,759 rows for the
onBehalfOf pull) -- so this script routes around Dune entirely rather than
spending down that shared budget.

Standard Aave v2 event, one indexed field:

    event ReserveDataUpdated(
        address indexed reserve,
        uint256 liquidityRate,
        uint256 stableBorrowRate,
        uint256 variableBorrowRate,
        uint256 liquidityIndex,
        uint256 variableBorrowIndex
    );

`topic0` (`keccak256("ReserveDataUpdated(address,uint256,uint256,uint256,uint256,uint256)")`)
was derived via `Crypto.Hash.keccak` (pycryptodome) then verified against a
real Etherscan `getLogs` result (a 500-block window around block 14,943,000)
before use: `topics[1]` decoded to a real reserve address, `data` was
exactly 160 bytes (5 words), and the decoded liquidityIndex/
variableBorrowIndex values (~1.08, ~1.13) were sane RAY-scaled (1e27)
growing-from-1.0 index values, not garbage.

Writes into the *same* directory `InterestIndexOracle` already globs
(`data/raw/aave_v2_reserve_index/chain=1/`), under a distinctly-named file
so it coexists with the 5 existing Dune-pulled golden-episode files rather
than colliding with them -- `InterestIndexOracle.__init__` concatenates
every parquet under that directory, so this extends coverage automatically,
no engine code changes needed (unlike Track B/C's corrections tables, this
one is a pure additive data extension of an already-wired-in oracle).

Usage:
    python scripts/onchain/backfill_reserve_index.py
Writes `data/raw/aave_v2_reserve_index/chain=1/reserve_data_updated_etherscan_full_history.parquet`
in `ReserveIndexIngester`'s existing schema (chain_id, block_number,
block_timestamp, reserve, liquidity_index_raw, variable_borrow_index_raw,
liquidity_rate_raw, variable_borrow_rate_raw, stable_borrow_rate_raw).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_svr_feed_events import get_logs_paginated  # noqa: E402

LENDING_POOL = "0x7d2768de32b0b80b7a3454c06bdac94a69ddc7a9"

# keccak256("ReserveDataUpdated(address,uint256,uint256,uint256,uint256,uint256)")
# -- verified live against real Etherscan getLogs results, see module docstring.
RESERVE_DATA_UPDATED_TOPIC0 = (
    "0x804c9b842b2748a22bb64b345453a3de7ca54a6ca45ce00d415894979e22897a"
)

_MIN_BLOCK = 11_500_000
# CAS-28 (H9 pre-work, 2026-07-23): bumped from 24_500_000 -- see the
# matching note in backfill_reserve_config_history.py. The event data this
# project reconstructs against runs to block 24,558,681; this only adds the
# new tail chunk(s), every prior chunk reloads from its checkpoint.
_MAX_BLOCK = 24_600_000
_CHUNK_SIZE = 250_000

_CHECKPOINT_DIR = Path("data/raw/.checkpoints")
_OUT_PARQUET = Path(
    "data/raw/aave_v2_reserve_index/chain=1/"
    "reserve_data_updated_etherscan_full_history.parquet"
)


def _chunks() -> list[tuple[int, int]]:
    bounds = list(range(_MIN_BLOCK, _MAX_BLOCK, _CHUNK_SIZE)) + [_MAX_BLOCK]
    return [(lo, hi - 1) for lo, hi in zip(bounds[:-1], bounds[1:])]


def _hex_to_int(value: str) -> int:
    """Same bare-`"0x"` zero-encoding quirk found in this ticket's other
    pull scripts -- normalize defensively here too."""
    return int(value, 16) if value not in ("0x", "", None) else 0


def _decode(logs: list[dict]) -> list[dict]:
    rows = []
    for log in logs:
        data = bytes.fromhex(log["data"][2:])
        words = [
            int.from_bytes(data[i : i + 32], "big") for i in range(0, len(data), 32)
        ]
        liquidity_rate, stable_rate, variable_rate, liquidity_index, variable_index = (
            words
        )
        rows.append(
            {
                "chain_id": 1,
                "block_number": _hex_to_int(log["blockNumber"]),
                "block_timestamp": pd.Timestamp(
                    _hex_to_int(log["timeStamp"]), unit="s", tz="UTC"
                ),
                "log_index": _hex_to_int(log["logIndex"]),
                "reserve": ("0x" + log["topics"][1][-40:]).lower(),
                "liquidity_index_raw": str(liquidity_index),
                "variable_borrow_index_raw": str(variable_index),
                "liquidity_rate_raw": str(liquidity_rate),
                "variable_borrow_rate_raw": str(variable_rate),
                "stable_borrow_rate_raw": str(stable_rate),
            }
        )
    return rows


def _pull_chunk(lo: int, hi: int, api_key: str) -> list[dict]:
    checkpoint = _CHECKPOINT_DIR / f"reserve_index_{lo}_{hi}.json"
    if checkpoint.exists():
        rows: list[dict] = json.loads(checkpoint.read_text())
        print(f"  [{lo},{hi}]: {len(rows)} rows (checkpoint)", flush=True)
        return rows

    logs = get_logs_paginated(
        LENDING_POOL,
        api_key,
        from_block=lo,
        to_block=hi,
        topic0=RESERVE_DATA_UPDATED_TOPIC0,
    )
    rows = _decode(logs)
    _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(json.dumps(rows, default=str))
    print(f"  [{lo},{hi}]: {len(rows)} rows (checkpointed)", flush=True)
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
            print(f"  retry [{lo},{hi}] in {wait}s after: {exc}", flush=True)
            time.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def main() -> None:
    api_key = os.environ["ETHERSCAN_API_KEY"]
    chunks = _chunks()
    print(
        f"Pulling ReserveDataUpdated over {len(chunks)} block chunks "
        f"[{_MIN_BLOCK}, {_MAX_BLOCK}]...",
        flush=True,
    )

    all_rows: list[dict] = []
    for lo, hi in chunks:
        all_rows.extend(_pull_chunk_with_retry(lo, hi, api_key))

    if not all_rows:
        raise RuntimeError("No ReserveDataUpdated logs pulled -- nothing to write")

    df = pd.DataFrame(all_rows)
    df = df.drop_duplicates(subset=["block_number", "log_index", "reserve"]).drop(
        columns=["log_index"]
    )
    df["block_number"] = df["block_number"].astype("int64")
    df["chain_id"] = df["chain_id"].astype("int32")
    df["block_timestamp"] = pd.to_datetime(df["block_timestamp"], utc=True)
    df = df.sort_values(["reserve", "block_number"], kind="mergesort").reset_index(
        drop=True
    )
    _OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(_OUT_PARQUET, index=False)
    print(f"\nWrote {len(df)} ReserveDataUpdated rows to {_OUT_PARQUET}")


if __name__ == "__main__":
    main()
