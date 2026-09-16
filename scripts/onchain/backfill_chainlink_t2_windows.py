"""Surgical Chainlink `AnswerUpdated` backfill around T2-mismatch trigger blocks (CAS-28).

The T2 gate (`cascadesignal.state.t2_gate`, PR #31) reconstructs HF at each
observed liquidation's trigger block and reports a ~28.5% mismatch rate vs
the T2 gate's 2% bar. Its dominant mismatch cause is
`unexplained_no_chainlink_coverage`: ~4,234 of ~6,427 mismatched triggers
have *no block-level Chainlink price at all*, because the existing Chainlink
pull (`data/raw/chainlink/chain=1/`) only covers the 5 golden-episode block
windows -- so `bucket_mismatch_causes` can't even test the oracle-lag
hypothesis for them (see that ticket's board note and `engine.py`'s docstring).

This script closes that blind spot *surgically*, not by pulling 5.5 years of
every feed. It:

  1. Re-runs the gate's own reconstruction + cause-bucketing over the real
     Aave v2 liquidation population (`build_pull_plan`), and collects the
     trigger blocks of every mismatch currently bucketed
     `unexplained_no_chainlink_coverage` **whose whole position is made of
     reserves that have a Chainlink feed at all** (i.e. the *fixable* subset;
     a position touching an `UNCOVERED_RESERVES` asset like stETH/GUSD can
     never be resolved by any pull and is skipped honestly).
  2. For each such (reserve, trigger-block), it needs the nearest-prior
     Chainlink price within `ChainlinkPriceOracle`'s 3-day `max_staleness`,
     so it pulls `[block - ~3 days of blocks, block]` per aggregator, merges
     overlapping windows (cascades cluster many liquidations into the same
     hours), and pulls each merged window via Etherscan `getLogs`.
  3. Writes the results into the *same* `data/raw/chainlink/chain=1/` lake in
     the canonical schema, so `ChainlinkPriceOracle` picks them up
     automatically on its next construction -- re-running the T2 diagnostics
     then reclassifies those triggers into `oracle_lag` (price staleness
     explained the mismatch) or `unexplained` (it didn't -> the real cause is
     param drift or an engine simplification, i.e. fix-path step 2/3).

Pull mechanics (Etherscan v2 `getLogs`, free tier, recursive 10k-window
bisection, per-aggregator JSON checkpointing) are reused wholesale from the
proven `fetch_svr_feed_events.py` -- same `AnswerUpdated` topic, same free
`ETHERSCAN_API_KEY`, same reason the free public RPCs don't work for
historical `eth_getLogs` (see that module's docstring). No Dune credits, no
RPC calls: aggregator decimals come from `RESERVE_CHAINLINK_FEEDS`, not an
`eth_call`.

Usage:
    python scripts/onchain/backfill_chainlink_t2_windows.py --dry-run   # plan only
    python scripts/onchain/backfill_chainlink_t2_windows.py             # plan + pull
Writes the plan to `experiments/T2/output/backfill_plan.json` and (non-dry)
`data/raw/chainlink/chain=1/t2_backfill_answer_updated.parquet`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd

# fetch_svr_feed_events lives beside this script; sys.path[0] is scripts/onchain
# when run directly, but add it explicitly so imports also work under pytest.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_svr_feed_events import get_logs_paginated  # noqa: E402

from cascadesignal.ingest.schema import normalize, to_arrow  # noqa: E402
from cascadesignal.state.chainlink_feeds import (  # noqa: E402
    ETH_USD_AGGREGATORS,
    RESERVE_CHAINLINK_FEEDS,
)
from cascadesignal.state.engine import PositionStateEngine, load_events  # noqa: E402
from cascadesignal.state.prices import ChainlinkPriceOracle, PriceOracle  # noqa: E402
from cascadesignal.state.reserves import reserve_table  # noqa: E402
from cascadesignal.state.t2_gate import (  # noqa: E402
    PriceOracleLike,
    bucket_mismatch_causes,
    reconstruct_hf_at_trigger,
)

# 3 days of blocks, matching `ChainlinkPriceOracle`'s `max_staleness`. Sized in
# *blocks* rather than time so it's era-independent: at the slowest historical
# ~13s/block this is still >3 days of wall-clock, and every feed here has a
# heartbeat well under 24h, so a window this wide is guaranteed to contain the
# nearest-prior update (which `max_staleness` then accepts). See module docstring.
STALENESS_BLOCKS = 21_600

_ETH_USD_ADDR = ETH_USD_AGGREGATORS[0].lower()
_CHECKPOINT_DIR = Path("data/raw/.checkpoints")
_OUT_PARQUET = Path("data/raw/chainlink/chain=1/t2_backfill_answer_updated.parquet")
_PLAN_PATH = Path("experiments/T2/output/backfill_plan.json")

NO_COVERAGE = "unexplained_no_chainlink_coverage"


def merge_windows(blocks: list[int], span: int = STALENESS_BLOCKS) -> list[list[int]]:
    """Collapse trigger blocks into merged `[block - span, block]` pull ranges.

    Cascades cluster many liquidations into a few hours, so most windows
    overlap heavily; merging them keeps the Etherscan request count near the
    number of *distinct episodes* rather than the number of liquidations.
    """
    merged: list[list[int]] = []
    for block in sorted(set(blocks)):
        lo, hi = block - span, block
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return merged


def build_pull_plan(
    engine: PositionStateEngine,
    liquidations: pd.DataFrame,
    price_oracle: PriceOracleLike,
    chainlink_oracle: PriceOracleLike | None,
) -> dict[str, dict]:
    """Compute the per-aggregator merged-window pull plan (see module docstring).

    Returns `{aggregator_address: {"symbol", "decimals", "windows": [[lo, hi]]}}`.
    Only fixable no-coverage triggers contribute -- a position touching any
    reserve absent from `RESERVE_CHAINLINK_FEEDS` (an `UNCOVERED_RESERVES`
    asset) is skipped, since no pull can ever cover it.
    """
    report, position_by_key = reconstruct_hf_at_trigger(
        engine, liquidations, price_oracle
    )
    causes = bucket_mismatch_causes(report, position_by_key, engine, chainlink_oracle)
    labeled = report.assign(cause=causes)
    no_cov = labeled[labeled["cause"] == NO_COVERAGE]

    blocks_by_addr: dict[str, set[int]] = defaultdict(set)
    meta_by_addr: dict[str, dict] = {}

    for _, row in no_cov.iterrows():
        key = (row["user"], int(row["block_number"]), int(row["log_index"]))
        reserves = [r.lower() for r in position_by_key[key]["reserve"].tolist()]
        if not all(r in RESERVE_CHAINLINK_FEEDS for r in reserves):
            continue  # unfixable: an uncovered reserve is in the position
        block = int(row["block_number"])
        for reserve in reserves:
            spec = RESERVE_CHAINLINK_FEEDS[reserve]
            for agg in spec["aggregators"]:
                addr = agg.lower()
                blocks_by_addr[addr].add(block)
                meta_by_addr.setdefault(
                    addr, {"symbol": spec["symbol"], "decimals": spec["decimals"]}
                )
            if spec["quote"] == "ETH":
                # ETH-quoted feeds need ETH/USD coverage at the same blocks to
                # be convertible to USD by ChainlinkPriceOracle.
                blocks_by_addr[_ETH_USD_ADDR].add(block)
                meta_by_addr.setdefault(
                    _ETH_USD_ADDR, {"symbol": "ETH/USD", "decimals": 8}
                )

    return {
        addr: {
            "symbol": meta_by_addr[addr]["symbol"],
            "decimals": meta_by_addr[addr]["decimals"],
            "windows": merge_windows(sorted(blocks)),
        }
        for addr, blocks in sorted(blocks_by_addr.items())
    }


def _rows_from_logs(aggregator: str, decimals: int, logs: list[dict]) -> list[dict]:
    """Canonical-schema rows for one aggregator's `AnswerUpdated` logs.

    Mirrors the existing `data/raw/chainlink/chain=1/` layout that
    `ChainlinkPriceOracle` reads: `protocol="chainlink"`, `user=<aggregator>`,
    `amount_raw=<int256 answer as string>`. `int256` is decoded from the
    first indexed topic as two's-complement (a price can't really go negative,
    but decode correctly regardless).
    """
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
                "user": aggregator.lower(),
                "collateral_asset": None,
                "debt_asset": None,
                "amount_raw": str(answer),
                "amount_usd": answer / (10.0**decimals),
                "liquidator": None,
                "collateral_seized_raw": None,
                "collateral_seized_usd": None,
            }
        )
    return rows


def _pull_aggregator(addr: str, spec: dict, api_key: str) -> list[dict]:
    """Pull every merged window for one aggregator, with a per-aggregator
    JSON checkpoint (deduped raw logs by (block, logIndex)) so an interrupted
    run resumes without re-hitting Etherscan.

    NB: the checkpoint bakes in *this run's* windows, which are derived from the
    current T2-mismatch population. If that population changes (e.g. after an
    engine fix), delete `data/raw/.checkpoints/chainlink_t2_*.json` before
    re-running so the new windows are actually pulled -- these are gitignored
    local scratch, not the canonical output (that's the parquet)."""
    checkpoint = _CHECKPOINT_DIR / f"chainlink_t2_{addr}.json"
    if checkpoint.exists():
        cached: list[dict] = json.loads(checkpoint.read_text())
        print(
            f"  {spec['symbol']:8s} {addr}: {len(cached)} rows (checkpoint)", flush=True
        )
        return cached

    seen: set[tuple[str, str]] = set()
    logs: list[dict] = []
    for lo, hi in spec["windows"]:
        for log in get_logs_paginated(addr, api_key, from_block=lo, to_block=hi):
            dedup_key = (log["blockNumber"], log["logIndex"])
            if dedup_key not in seen:
                seen.add(dedup_key)
                logs.append(log)
    _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(json.dumps(logs))
    print(
        f"  {spec['symbol']:8s} {addr}: {len(logs)} rows over "
        f"{len(spec['windows'])} windows (checkpointed)",
        flush=True,
    )
    return logs


def _pull_aggregator_with_retry(
    addr: str, spec: dict, api_key: str, attempts: int = 4
) -> list[dict]:
    """`_pull_aggregator` but resilient to a transient Etherscan failure
    exhausting `get_logs_paginated`'s own per-request retries (which raises and
    would otherwise abort the whole 2,991-window run). A failed aggregator has
    no checkpoint yet, so retrying re-pulls it cleanly; already-checkpointed
    aggregators short-circuit on the first call regardless."""
    for attempt in range(attempts):
        try:
            return _pull_aggregator(addr, spec, api_key)
        except RuntimeError as exc:
            if attempt == attempts - 1:
                raise
            wait = 30 * (attempt + 1)
            print(f"  retry {addr} in {wait}s after: {exc}", flush=True)
            time.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def _write_plan(plan: dict[str, dict]) -> None:
    n_windows = sum(len(p["windows"]) for p in plan.values())
    summary = {
        "staleness_blocks": STALENESS_BLOCKS,
        "n_aggregators": len(plan),
        "n_windows": n_windows,
        "aggregators": plan,
    }
    _PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PLAN_PATH.write_text(json.dumps(summary, indent=2))
    print(
        f"\nPlan: {len(plan)} aggregators, {n_windows} merged windows "
        f"(~3-day span each). Wrote {_PLAN_PATH}"
    )
    for addr, p in sorted(plan.items(), key=lambda kv: -len(kv[1]["windows"])):
        print(f"  {p['symbol']:8s} {addr}  windows={len(p['windows'])}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute + write the pull plan but make no Etherscan calls.",
    )
    args = parser.parse_args()

    reserves = reserve_table()
    events = load_events(data_dir="data/raw", protocol="aave_v2")
    liquidations = events[events["event_type"] == "LiquidationCall"]
    engine = PositionStateEngine(events=events, reserve_table=reserves)

    try:
        chainlink_oracle: ChainlinkPriceOracle | None = ChainlinkPriceOracle()
    except FileNotFoundError:
        chainlink_oracle = None

    plan = build_pull_plan(engine, liquidations, PriceOracle(), chainlink_oracle)
    _write_plan(plan)
    if args.dry_run:
        return

    api_key = os.environ["ETHERSCAN_API_KEY"]
    all_rows: list[dict] = []
    for addr, spec in plan.items():
        logs = _pull_aggregator_with_retry(addr, spec, api_key)
        all_rows.extend(_rows_from_logs(addr, spec["decimals"], logs))

    if not all_rows:
        print("No AnswerUpdated rows pulled -- nothing to write.")
        return

    df = normalize(pd.DataFrame(all_rows), protocol="chainlink")
    df = df.drop_duplicates(subset=["user", "block_number", "log_index"])
    _OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    to_arrow(df).to_pandas().to_parquet(_OUT_PARQUET, index=False)
    print(f"\nWrote {len(df)} rows to {_OUT_PARQUET}")


if __name__ == "__main__":
    main()
