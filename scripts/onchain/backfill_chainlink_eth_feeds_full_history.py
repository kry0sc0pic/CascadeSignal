"""Full-history `AnswerUpdated` pull for every Aave v2 asset/ETH Chainlink
feed (CAS-28 H2).

H1 (`RESERVE_CHAINLINK_ETH_FEEDS`) only has events from the 5 golden-episode
block windows (~250 days total), because that's all the original pull
covered. H2's phase discovery (`discover_chainlink_eth_feed_phases.py`) then
found each feed's *complete* on-chain phase history, including migrations
that happened between episodes. This script pulls **every** `AnswerUpdated`
event for **every** aggregator address now in `RESERVE_CHAINLINK_ETH_FEEDS`
(known + newly discovered) across the full study period
[11,362,000, chain tip] -- not just the golden windows -- so
`EthNumeraire`/`ChainlinkPriceOracle`-style nearest-prior lookups stop
falling through to `unexplained_no_chainlink_coverage` outside those windows.

Pull mechanics reused wholesale from `fetch_svr_feed_events.py` /
`backfill_chainlink_t2_windows.py`: Etherscan v2 `getLogs`
(`ETHERSCAN_API_KEY`, free tier), recursive 10,000-row-window bisection,
per-aggregator-address JSON checkpointing (an interrupted run resumes
without re-pulling already-checkpointed addresses). Decimals come from
`RESERVE_CHAINLINK_ETH_FEEDS` directly (always 18 for asset/ETH feeds) --
no extra `eth_call` needed.

Usage:
    python scripts/onchain/backfill_chainlink_eth_feeds_full_history.py --dry-run
    python scripts/onchain/backfill_chainlink_eth_feeds_full_history.py
Writes `data/raw/chainlink/chain=1/eth_feeds_full_history_answer_updated.parquet`.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_svr_feed_events import get_logs_paginated  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from cascadesignal.ingest.schema import normalize, to_arrow  # noqa: E402
from cascadesignal.state.chainlink_feeds import (
    RESERVE_CHAINLINK_ETH_FEEDS,
)  # noqa: E402

RPC_URL = "https://ethereum-rpc.publicnode.com"
_CHECKPOINT_DIR = Path("data/raw/.checkpoints")
_OUT_PARQUET = Path(
    "data/raw/chainlink/chain=1/eth_feeds_full_history_answer_updated.parquet"
)

# Aave v2's actual launch block (see backfill_aave_v2_launch_events.py); the
# upper bound is resolved live against the chain tip at run time.
FULL_HISTORY_START_BLOCK = 11_362_000


def get_block_number() -> int:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": "eth_blockNumber",
        "params": [],
        "id": 1,
    }
    resp = requests.post(RPC_URL, json=payload, timeout=20)
    return int(resp.json()["result"], 16)


def build_plan() -> dict[str, dict]:
    """address -> {symbol, decimals} for every aggregator across all 30
    covered reserves (known H1 addresses + H2-discovered phases alike --
    same full-history pull regardless of which pass found the address)."""
    plan: dict[str, dict] = {}
    for _reserve, spec in RESERVE_CHAINLINK_ETH_FEEDS.items():
        for addr in spec["aggregators"]:
            plan[addr.lower()] = {
                "symbol": spec["symbol"],
                "decimals": spec["decimals"],
            }
    return plan


def _rows_from_logs(aggregator: str, decimals: int, logs: list[dict]) -> list[dict]:
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


def _pull_aggregator(
    addr: str, symbol: str, api_key: str, from_block: int, to_block: int
) -> list[dict]:
    checkpoint = _CHECKPOINT_DIR / f"chainlink_eth_full_{addr}.json"
    if checkpoint.exists():
        cached: list[dict] = json.loads(checkpoint.read_text())
        print(f"  {symbol:8s} {addr}: {len(cached)} rows (checkpoint)", flush=True)
        return cached

    logs = get_logs_paginated(addr, api_key, from_block=from_block, to_block=to_block)
    _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(json.dumps(logs))
    print(f"  {symbol:8s} {addr}: {len(logs)} rows (checkpointed)", flush=True)
    return logs


def _pull_aggregator_with_retry(
    addr: str,
    symbol: str,
    api_key: str,
    from_block: int,
    to_block: int,
    attempts: int = 4,
) -> list[dict]:
    for attempt in range(attempts):
        try:
            return _pull_aggregator(addr, symbol, api_key, from_block, to_block)
        except RuntimeError as exc:
            if attempt == attempts - 1:
                raise
            wait = 30 * (attempt + 1)
            print(f"  retry {addr} in {wait}s after: {exc}", flush=True)
            time.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the plan, pull nothing."
    )
    args = parser.parse_args()

    plan = build_plan()
    chain_tip = get_block_number()
    print(
        f"Plan: {len(plan)} aggregator addresses, blocks "
        f"[{FULL_HISTORY_START_BLOCK}, {chain_tip}] (chain tip)\n"
    )
    for addr, spec in sorted(plan.items(), key=lambda kv: kv[1]["symbol"]):
        print(f"  {spec['symbol']:8s} {addr}")
    if args.dry_run:
        return

    api_key = os.environ["ETHERSCAN_API_KEY"]
    all_rows: list[dict] = []
    for i, (addr, spec) in enumerate(sorted(plan.items()), start=1):
        print(f"[{i}/{len(plan)}]", end=" ", flush=True)
        logs = _pull_aggregator_with_retry(
            addr, spec["symbol"], api_key, FULL_HISTORY_START_BLOCK, chain_tip
        )
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
