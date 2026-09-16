"""Pull Aave v2's full-history reserve risk-parameter changes
(`CollateralConfigurationChanged`) via Etherscan (CAS-28, Lever 3).

`reserves.py`'s `ONCHAIN_RESERVE_CONFIG` is Aave v2's *current, frozen* risk
parameters (pinned to block 25,479,510). Aave v2 is now deprecated and most
reserves were de-risked toward a near-zero `liquidation_threshold` before the
freeze, so those current values are NOT representative of the 2021-2022 study
period (see `reserves.py`'s docstring). The T2 gate reconstructs HF at each
historical liquidation's trigger block, so it needs the liquidation threshold
*in effect at that block*, not the frozen one.

`LendingPoolConfigurator.configureReserveAsCollateral(asset, ltv,
liquidationThreshold, liquidationBonus)` emits, on every governance change:

    event CollateralConfigurationChanged(
        address indexed asset,
        uint256 ltv,                  // bps, 10000 = 100%
        uint256 liquidationThreshold, // bps
        uint256 liquidationBonus      // bps over par, 11500 = +15% bonus
    );

`topic0` (`keccak256("CollateralConfigurationChanged(address,uint256,uint256,uint256)")`)
and the configurator proxy address `0x311bb771...` were both verified live
against a real Etherscan `getLogs` result before use (a hit at block
11,538,358 decoded to CRV with ltv=4000/lt=5500/bonus=11500 bps -- sane
initial-listing values). This is a low-frequency governance event (a few
hundred rows across all reserves over 5 years, not the millions
`ReserveDataUpdated` emits), so the whole [11.3M, 24.6M] range pulls quickly.

Writes `data/raw/aave_v2_reserve_config/chain=1/collateral_configuration_changed.parquet`
(chain_id, block_number, block_timestamp, asset, ltv, liquidation_threshold,
liquidation_bonus -- the latter three as fractions, matching `reserves.py`'s
conventions: ltv/lt are plain fractions, bonus is the fraction *over* par).

Usage:
    python scripts/onchain/backfill_reserve_config_history.py
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

LENDING_POOL_CONFIGURATOR = "0x311bb771e4f8952e6da169b425e7e92d6ac45756"

# keccak256("CollateralConfigurationChanged(address,uint256,uint256,uint256)")
# -- verified live against a real Etherscan getLogs result, see module docstring.
COLLATERAL_CONFIG_CHANGED_TOPIC0 = (
    "0x637febbda9275aea2e85c0ff690444c8d87eb2e8339bbede9715abcc89cb0995"
)

# Aave v2 mainnet launched ~block 11,362,000 (Dec 2020); the initial
# `configureReserveAsCollateral` listings emit here, so start below that to
# capture every reserve's *first* (listing-time) threshold, not just later
# governance changes -- China-episode (May 2021) reconstructions need it.
_MIN_BLOCK = 11_300_000
# CAS-28 (H9 pre-work, 2026-07-23): bumped from 24_500_000 -- the event data
# this project actually reconstructs against runs to block 24,558,681
# (2026-02-28, the study period's real end), so the previous cap silently
# left every liquidation in that ~58k-block tail using whatever threshold
# was last recorded before 24.5M. Checkpoints confirm every chunk through
# 24,499,999 was already pulled to completion (not a truncated prior run);
# this only adds the one new tail chunk.
_MAX_BLOCK = 24_600_000
_CHUNK_SIZE = 1_000_000

_CHECKPOINT_DIR = Path("data/raw/.checkpoints")
_OUT_PARQUET = Path(
    "data/raw/aave_v2_reserve_config/chain=1/"
    "collateral_configuration_changed.parquet"
)


def _chunks() -> list[tuple[int, int]]:
    bounds = list(range(_MIN_BLOCK, _MAX_BLOCK, _CHUNK_SIZE)) + [_MAX_BLOCK]
    return [(lo, hi - 1) for lo, hi in zip(bounds[:-1], bounds[1:])]


def _hex_to_int(value: str) -> int:
    return int(value, 16) if value not in ("0x", "", None) else 0


def _decode(logs: list[dict]) -> list[dict]:
    rows = []
    for log in logs:
        data = bytes.fromhex(log["data"][2:])
        words = [
            int.from_bytes(data[i : i + 32], "big") for i in range(0, len(data), 32)
        ]
        ltv_bps, lt_bps, bonus_bps = words[0], words[1], words[2]
        rows.append(
            {
                "chain_id": 1,
                "block_number": _hex_to_int(log["blockNumber"]),
                "block_timestamp": pd.Timestamp(
                    _hex_to_int(log["timeStamp"]), unit="s", tz="UTC"
                ),
                "log_index": _hex_to_int(log["logIndex"]),
                "asset": ("0x" + log["topics"][1][-40:]).lower(),
                "ltv": ltv_bps / 10_000.0,
                "liquidation_threshold": lt_bps / 10_000.0,
                # Aave stores bonus as bps *over* par (11500 = +15%); reserves.py
                # keeps the fraction over par, and 0 when the reserve can't be
                # liquidated collateral (bonus unset).
                "liquidation_bonus": (bonus_bps / 10_000.0 - 1.0) if bonus_bps else 0.0,
            }
        )
    return rows


def _pull_chunk(lo: int, hi: int, api_key: str) -> list[dict]:
    checkpoint = _CHECKPOINT_DIR / f"reserve_config_{lo}_{hi}.json"
    if checkpoint.exists():
        rows: list[dict] = json.loads(checkpoint.read_text())
        print(f"  [{lo},{hi}]: {len(rows)} rows (checkpoint)", flush=True)
        return rows

    logs = get_logs_paginated(
        LENDING_POOL_CONFIGURATOR,
        api_key,
        from_block=lo,
        to_block=hi,
        topic0=COLLATERAL_CONFIG_CHANGED_TOPIC0,
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
        f"Pulling CollateralConfigurationChanged over {len(chunks)} block chunks "
        f"[{_MIN_BLOCK}, {_MAX_BLOCK}]...",
        flush=True,
    )

    all_rows: list[dict] = []
    for lo, hi in chunks:
        all_rows.extend(_pull_chunk_with_retry(lo, hi, api_key))

    if not all_rows:
        raise RuntimeError("No CollateralConfigurationChanged logs pulled")

    df = pd.DataFrame(all_rows)
    df = df.drop_duplicates(subset=["block_number", "log_index", "asset"]).drop(
        columns=["log_index"]
    )
    df["block_number"] = df["block_number"].astype("int64")
    df["chain_id"] = df["chain_id"].astype("int32")
    df["block_timestamp"] = pd.to_datetime(df["block_timestamp"], utc=True)
    df = df.sort_values(["asset", "block_number"], kind="mergesort").reset_index(
        drop=True
    )
    _OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(_OUT_PARQUET, index=False)
    print(f"\nWrote {len(df)} CollateralConfigurationChanged rows to {_OUT_PARQUET}")


if __name__ == "__main__":
    main()
