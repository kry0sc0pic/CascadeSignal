"""Pull Chainlink SVR-aggregator `AnswerUpdated` events for Aave-dedicated SVR feeds (CAS-47).

Chainlink SVR (Smart Value Recapture) routes price updates through a *second*
feed alongside the standard one -- the standard feed still updates via the
public mempool as before, while the SVR feed updates via a private Flashbots
MEV-Share auction, letting a searcher backrun the fresh price with a
liquidation in the *same block* (see docs.chain.link/data-feeds/svr-feeds).
The already-pulled golden-episode Chainlink data (`data/raw/chainlink/chain=1/`)
only covers 32 Aave v2 reserves' *standard* aggregators -- none of the SVR
addresses below appear anywhere in it, so SVR-path detection needs its own,
separate pull.

Two-step address resolution (see `cascadesignal.state.svr`'s module
docstring for the full story): Chainlink's public feed-address page lists
each SVR feed's stable `svr_proxy` (an `EACAggregatorProxy`-style contract),
but that proxy does NOT itself emit `AnswerUpdated` -- like every other
Chainlink feed in this repo, it delegates to an underlying aggregator,
resolved once via each proxy's `aggregator()` view function (a free
`eth_call` against `"latest"`, which works fine even though historical
`eth_getLogs` doesn't -- see the blocker note below). `svr_aggregator` in
`AAVE_SVR_FEEDS` is that resolved, real event-emitting address.

Historical pull source: **Etherscan's v2 `getLogs` API**, not a free public
RPC. `ethereum-rpc.publicnode.com` (used by this repo's other on-chain
scripts) rejects every non-`latest`-anchored `eth_getLogs` call with
"Archive requests require a personal token" -- confirmed even for a 50-block
window 1000 blocks behind the chain tip. `rpc.ankr.com` needs its own key,
`1rpc.io` caps every call to a 50-block range, `llamarpc.com`/`drpc.org` are
unreachable. Etherscan's v2 `getLogs` (`ETHERSCAN_API_KEY`, free tier) has
none of those limits and returns `timeStamp` directly per log, avoiding a
separate `eth_getBlockByNumber` pass. Also not a Dune pull: both keys tried
this session hit `402 "Max number of private queries reached"` (an
account-level cap on saved private queries, unrelated to credit balance).

Etherscan also caps every single query's result *window* at 10,000
(`page * offset <= 10000`) -- silently returning exactly 10,000 rows rather
than erroring when a range has more. Confirmed this bit 4 of the 6 feeds on
a naive single-range pull (their returned rows all had a suspiciously early
max `block_timestamp`). Worked around by recursively bisecting any range
that returns exactly the cap.

Checkpointed per-feed under `data/raw/.checkpoints/svr_logs_{symbol}.json`
(raw Etherscan log dicts) -- a feed already checkpointed is skipped on
re-run. This pull is slow (network-bound, one of the 6 feeds needed ~1000+
paginated + bisected requests against a rate-limited free API), so losing
all progress on an interrupted run would be costly.

Usage:
    python scripts/onchain/fetch_svr_feed_events.py
Writes `data/raw/chainlink_svr/chain=1/svr_answer_updated.parquet` in the
canonical event schema (cascadesignal.ingest.schema).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from cascadesignal.ingest.schema import normalize, to_arrow
from cascadesignal.state.svr import AAVE_SVR_FEEDS

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
RPC_URL = "https://ethereum-rpc.publicnode.com"
CHECKPOINT_DIR = Path("data/raw/.checkpoints")

# keccak256("AnswerUpdated(int256,uint256,uint256)"), derived once via
# pycryptodome's Crypto.Hash.keccak (not memorized) -- standard Chainlink
# AggregatorV3Interface event, same event this repo already decodes via Dune
# in scripts/dune/chainlink_prices.sql.
ANSWER_UPDATED_TOPIC0 = (
    "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
)

SELECTOR_DECIMALS = "313ce567"
_MAX_RESULTS_PER_PAGE = 1000
_ETHERSCAN_RESULT_WINDOW_CAP = 10_000  # page * offset must stay <= this


def _rpc(method: str, params: list[Any], retries: int = 5) -> Any:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }
    result: dict = {}
    for attempt in range(retries):
        try:
            resp = requests.post(RPC_URL, json=payload, timeout=30)
            result = resp.json()
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
            continue
        if "error" in result:
            time.sleep(1.5 * (attempt + 1))
            continue
        return result["result"]
    raise RuntimeError(f"{method} failed after {retries} retries: {result}")


def get_decimals(address: str) -> int:
    hex_data = _rpc(
        "eth_call", [{"to": address, "data": "0x" + SELECTOR_DECIMALS}, "latest"]
    )
    return int(hex_data, 16)


def _etherscan_get_logs(
    address: str,
    api_key: str,
    from_block: int,
    to_block: int,
    page: int,
    topic0: str = ANSWER_UPDATED_TOPIC0,
    retries: int = 4,
) -> list[dict] | None:
    """One paginated Etherscan call, with retry/backoff. Returns None (not an
    empty list) on a request/JSON failure after all retries, distinguishing
    "genuinely no more results" from "the request kept failing" -- callers
    must not treat a None as end-of-results."""
    params: dict[str, Any] = {
        "chainid": 1,
        "module": "logs",
        "action": "getLogs",
        "address": address,
        "topic0": topic0,
        "fromBlock": from_block,
        "toBlock": to_block,
        "page": page,
        "offset": _MAX_RESULTS_PER_PAGE,
        "apikey": api_key,
    }
    for attempt in range(retries):
        try:
            resp = requests.get(ETHERSCAN_URL, params=params, timeout=20)
            payload = resp.json()
        except (requests.RequestException, ValueError):
            time.sleep(2.0 * (attempt + 1))
            continue
        result = payload.get("result")
        if isinstance(result, list):
            return result
        if payload.get("message") == "No records found":
            return []
        # Rate-limited or a transient API error -- back off and retry.
        time.sleep(2.0 * (attempt + 1))
    return None


_MAX_PAGE = (
    _ETHERSCAN_RESULT_WINDOW_CAP // _MAX_RESULTS_PER_PAGE
)  # page 11+ = page*offset > 10000, hard-rejected


def _get_logs_page(
    address: str,
    api_key: str,
    from_block: int,
    to_block: int,
    topic0: str = ANSWER_UPDATED_TOPIC0,
) -> list[dict]:
    logs: list[dict] = []
    page = 1
    while page <= _MAX_PAGE:
        result = _etherscan_get_logs(
            address, api_key, from_block, to_block, page, topic0=topic0
        )
        if result is None:
            raise RuntimeError(
                f"Etherscan getLogs kept failing for {address} "
                f"[{from_block}, {to_block}] page {page}"
            )
        if not result:
            break
        logs.extend(result)
        if len(result) < _MAX_RESULTS_PER_PAGE:
            break
        page += 1
        time.sleep(0.25)  # be polite to the free-tier rate limit
    # Stopping at _MAX_PAGE with a full last page means we hit the window
    # cap, not that there are exactly 10,000 real results -- the caller
    # (get_logs_paginated) checks len(logs) and bisects the range if so.
    return logs


def get_logs_paginated(
    address: str,
    api_key: str,
    from_block: int = 0,
    to_block: int = 99_999_999,
    topic0: str = ANSWER_UPDATED_TOPIC0,
) -> list[dict]:
    """Fetch every matching log in [from_block, to_block], working around
    Etherscan's hard 10,000-result window per query by recursively bisecting
    the block range whenever a query returns exactly the cap -- see module
    docstring. `topic0` defaults to `AnswerUpdated` (this module's original
    purpose) but any event topic works -- reused as-is by
    `fix_gateway_onbehalfof.py` for Aave v2's `Deposit`/`Borrow` topics."""
    logs = _get_logs_page(address, api_key, from_block, to_block, topic0=topic0)
    if len(logs) < _ETHERSCAN_RESULT_WINDOW_CAP or from_block >= to_block:
        return logs
    mid = (from_block + to_block) // 2
    left = get_logs_paginated(address, api_key, from_block, mid, topic0=topic0)
    right = get_logs_paginated(address, api_key, mid + 1, to_block, topic0=topic0)
    return left + right


def pull_feed(symbol: str, aggregator: str, api_key: str) -> list[dict]:
    checkpoint_path = CHECKPOINT_DIR / f"svr_logs_{symbol}.json"
    if checkpoint_path.exists():
        logs = json.loads(checkpoint_path.read_text())
        print(f"  {symbol}: {len(logs)} rows (from checkpoint)", flush=True)
        return logs

    print(f"Fetching {symbol} SVR aggregator {aggregator}...", flush=True)
    logs = get_logs_paginated(aggregator, api_key)
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(json.dumps(logs))
    print(f"  {symbol}: {len(logs)} AnswerUpdated rows (checkpointed)", flush=True)
    return logs


def _rows_from_logs(
    reserve: str, aggregator: str, decimals: int, logs: list[dict]
) -> list[dict]:
    rows = []
    for log in logs:
        current_raw = int(log["topics"][1], 16)
        # int256 indexed as a two's-complement 32-byte topic.
        if current_raw >= 2**255:
            current_raw -= 2**256
        rows.append(
            {
                "chain_id": 1,
                "block_number": int(log["blockNumber"], 16),
                "block_timestamp": pd.Timestamp(
                    int(log["timeStamp"], 16), unit="s", tz="UTC"
                ),
                "tx_hash": log["transactionHash"],
                "log_index": int(log["logIndex"], 16),
                "protocol": "chainlink_svr",
                "event_type": "AnswerUpdated",
                "user": aggregator.lower(),
                "collateral_asset": reserve,
                "debt_asset": None,
                "amount_raw": str(current_raw),
                "amount_usd": current_raw / (10.0**decimals),
                "liquidator": None,
                "collateral_seized_raw": None,
                "collateral_seized_usd": None,
            }
        )
    return rows


def main() -> None:
    api_key = os.environ["ETHERSCAN_API_KEY"]

    all_rows: list[dict] = []
    for reserve, spec in AAVE_SVR_FEEDS.items():
        symbol, aggregator = spec["symbol"], spec["svr_aggregator"]
        logs = pull_feed(symbol, aggregator, api_key)
        if not logs:
            continue
        decimals = get_decimals(aggregator)
        all_rows.extend(_rows_from_logs(reserve, aggregator, decimals, logs))

    if not all_rows:
        raise RuntimeError("No SVR AnswerUpdated events found")

    df = normalize(pd.DataFrame(all_rows), protocol="chainlink_svr")
    out_dir = Path("data/raw/chainlink_svr/chain=1")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "svr_answer_updated.parquet"
    to_arrow(df).to_pandas().to_parquet(out_path, index=False)
    print(f"Wrote {len(df)} rows to {out_path}")


if __name__ == "__main__":
    main()
