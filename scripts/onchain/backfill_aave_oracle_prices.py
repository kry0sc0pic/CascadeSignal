"""Backfill Aave v2 `AaveOracle.getAssetPrice` into the live-oracle cache for
the reserves Chainlink log reconstruction cannot price at all (CAS-28).

`chainlink_feeds.UNCOVERED_RESERVES` lists the 5 reserves with zero matching
`AnswerUpdated` rows anywhere in the pulled Chainlink lake (GUSD, xSUSHI,
stETH, ENS, CVX). `BlendedPriceOracle` therefore falls all the way through to
`PriceOracle`'s DefiLlama *daily* series for them -- day-level resolution on
~15% of the liquidation population (7,398 triggers hold one as collateral,
1,367 as debt). stETH is the worst case: its Terra-window depeg is an
intraday move that a daily price cannot represent at all.

Aave's own deployed oracle can price them -- it reflects whatever source Aave
actually read at that block, including the internal fallback-oracle mechanism
and adapters this project never mapped (see `LiveAaveOracleFallback`). This
script pre-populates that oracle's cache for exactly those (asset, block)
pairs, so the prices are available offline afterward like every other source.

Deliberately NOT a full-population sweep. On rows the existing reconstruction
already gets right, the live oracle agrees to a median 0.22% price difference
(CAS-28) -- repricing all ~29.4k trigger blocks would cost ~100k archive
`eth_call`s to change almost nothing, and would make the T2 number depend on
an RPC endpoint instead of committed logs. Scope stays on the reserves that
have no feed at all.

Queries at `block_number - 1`, this project's pre-liquidation convention
(same as `t2_gate.apply_live_oracle_fallback` and H7's `getUserAccountData`
spot-check).

Resumable: `LiveAaveOracleFallback` persists every (address, block) result to
its parquet cache as it goes, so an interrupted run loses nothing and a re-run
only pays for what is still missing.

Usage:
    python scripts/onchain/backfill_aave_oracle_prices.py --dry-run
    python scripts/onchain/backfill_aave_oracle_prices.py --limit 500
    python scripts/onchain/backfill_aave_oracle_prices.py --assets stETH,ENS
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import pandas as pd

from cascadesignal.labels.cascade_labeler import load_liquidations
from cascadesignal.state.chainlink_feeds import UNCOVERED_RESERVES
from cascadesignal.state.prices import LiveAaveOracleFallback

# Same default `LiveAaveOracleFallback` uses; named here so --dry-run can read
# the cache without constructing the oracle (which needs no key, but would
# also not surface the count).
_CACHE_PATH = Path("data/raw/aave_oracle_live/chain=1/asset_price_cache.parquet")
_DATA_DIR = Path("data/raw")


def _require_key() -> None:
    """`_fetch_live` returns None when ARCHIVE_RPC_URL is unset, and `price_at`
    caches that None permanently -- indistinguishable afterward from a real
    "no source configured" revert. Refuse to run keyless rather than poison
    the cache with fake no-answers."""
    if not os.environ.get("ARCHIVE_RPC_URL"):
        sys.exit(
            "ARCHIVE_RPC_URL is not set. Refusing to run: without it every "
            "lookup would cache a null price permanently. Set it in .env and "
            "`set -a && source .env && set +a` first."
        )


def _target_pairs(assets: dict[str, str]) -> pd.DataFrame:
    """Distinct (address, block_number - 1) pairs where a liquidation touches
    one of `assets` on either leg."""
    liq = load_liquidations(_DATA_DIR, ["aave_v2"])
    wanted = set(assets)
    frames = []
    for leg in ("collateral_asset", "debt_asset"):
        hit = liq[liq[leg].str.lower().isin(wanted)]
        frames.append(
            pd.DataFrame(
                {
                    "address": hit[leg].str.lower(),
                    "block_number": hit["block_number"].astype("int64") - 1,
                }
            )
        )
    pairs = pd.concat(frames, ignore_index=True).drop_duplicates()
    return pairs.sort_values(["address", "block_number"], kind="mergesort").reset_index(
        drop=True
    )


def _cached_keys() -> set[tuple[str, int]]:
    if not _CACHE_PATH.exists():
        return set()
    cached = pd.read_parquet(_CACHE_PATH)
    return set(
        zip(
            cached["address"].astype(str).str.lower(),
            cached["block_number"].astype(int),
        )
    )


def _evict_nulls() -> list[tuple[str, int]]:
    """Drop every null-price row from the cache and return its (address,
    block) pairs, so the normal fetch path re-queries them.

    `price_at` short-circuits on any key already present -- including one
    holding `None` -- so a null cached by a transient failure is permanent
    until evicted. `_fetch_live` used to match reverts on a loose
    `"revert" in message or "execution" in message` check that misread
    rate-limit errors as contract reverts (see its comment); rows cached
    before that fix was narrowed to `"execution reverted"` are still stuck.
    A null on a reserve that is priced at thousands of other blocks cannot
    be a genuine "no source configured" revert.
    """
    cached = pd.read_parquet(_CACHE_PATH)
    nulls = cached[cached["price"].isna()]
    pairs = [
        (a, int(b))
        for a, b in zip(
            nulls["address"].astype(str).str.lower(), nulls["block_number"].astype(int)
        )
    ]
    cached[cached["price"].notna()].to_parquet(_CACHE_PATH, index=False)
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--assets",
        default=None,
        help="Comma-separated symbols from UNCOVERED_RESERVES (default: all 5)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max calls this run")
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Seconds between calls, for rate-limited endpoints",
    )
    parser.add_argument(
        "--retry-nulls",
        action="store_true",
        help="Re-query every null-price row already in the cache (see _evict_nulls)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many pairs are missing, fetch nothing",
    )
    args = parser.parse_args()

    if args.retry_nulls:
        cached = pd.read_parquet(_CACHE_PATH)
        n_null = int(cached["price"].isna().sum())
        print(f"{n_null} null-price rows cached out of {len(cached)}", flush=True)
        if args.dry_run or not n_null:
            return
        _require_key()
        backup = _CACHE_PATH.with_suffix(".parquet.bak")
        cached.to_parquet(backup, index=False)
        print(f"backed up cache to {backup}", flush=True)
        missing = _evict_nulls()
    else:
        assets = {a.lower(): s for a, s in UNCOVERED_RESERVES.items()}
        if args.assets:
            want = {s.strip().lower() for s in args.assets.split(",")}
            assets = {a: s for a, s in assets.items() if s.lower() in want}
            if not assets:
                sys.exit(f"No UNCOVERED_RESERVES match --assets {args.assets}")

        pairs = _target_pairs(assets)
        cached_keys = _cached_keys()
        missing = [
            (a, int(b))
            for a, b in zip(pairs["address"], pairs["block_number"])
            if (a, int(b)) not in cached_keys
        ]

        print(f"Targets: {', '.join(sorted(assets.values()))}", flush=True)
        print(
            f"{len(pairs)} distinct (asset, block) pairs -- "
            f"{len(pairs) - len(missing)} already cached, {len(missing)} to fetch",
            flush=True,
        )
        if args.dry_run or not missing:
            return
        _require_key()

    todo = missing[: args.limit] if args.limit else missing
    oracle = LiveAaveOracleFallback()
    timestamp = pd.Timestamp.now("UTC")  # unused by this oracle; block pins the call
    priced = 0
    for i, (address, block_number) in enumerate(todo, start=1):
        price = oracle.price_at(address, timestamp, int(block_number))
        if price is not None:
            priced += 1
        if i % 100 == 0 or i == len(todo):
            print(
                f"  {i}/{len(todo)} fetched -- {priced} priced, "
                f"{i - priced} no answer ({oracle.n_live_calls} rpc calls)",
                flush=True,
            )
        if args.sleep:
            time.sleep(args.sleep)

    print(
        f"\nDone. {priced}/{len(todo)} pairs resolved to a price; cache at "
        f"{_CACHE_PATH}. {len(missing) - len(todo)} pairs still missing.",
        flush=True,
    )


if __name__ == "__main__":
    main()
