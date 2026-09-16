"""JSON-RPC + Etherscan getLogs helpers for the live monitor.

Two log sources, used for different jobs (both verified live against
`ARCHIVE_RPC_URL`, an Alchemy free-tier endpoint, on 2026-08-04):

* `ws_subscribe_logs` -- `eth_subscribe("logs", ...)` over `wss://`, for
  near-real-time tailing of new liquidations. Confirmed working (received a
  live `newHeads` push in <20s).
* `etherscan_get_logs_paginated` -- Etherscan's v2 `getLogs`, for the wide
  block-range catch-up backfill on startup/reconnect. Required because
  Alchemy's free tier caps `eth_getLogs` at a **10-block range per call**
  (confirmed live: a 50,000-block range was rejected with "Under the Free
  tier plan, you can make eth_getLogs requests with up to a 10 block
  range"), making Alchemy useless for a multi-month backfill gap. Etherscan
  v2 has no such range cap, only a 10,000-row *result window* per query
  (`page*offset <= 10000`) -- worked around by recursively bisecting any
  range that returns exactly the cap, the same pattern
  `scripts/onchain/fetch_svr_feed_events.py:get_logs_paginated` already
  uses (reimplemented here, not imported, since that module lives under
  `scripts/` and is invoked as a standalone CLI script, not a package this
  installable `src/` layout should reach into).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import requests
import websockets

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
_ETHERSCAN_RESULT_WINDOW_CAP = 10_000
_ETHERSCAN_PAGE_SIZE = 1000


def http_rpc(url: str, method: str, params: list[Any], timeout: float = 20.0) -> Any:
    """One JSON-RPC call over HTTPS. Raises on transport or JSON-RPC error."""
    resp = requests.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"RPC error for {method}: {payload['error']}")
    return payload["result"]


def get_finalized_block(url: str) -> int:
    """The chain's finalized head (post-Merge Ethereum finality tag) -- the
    live scoring boundary: a bar is only closed/scored once every block in
    it is at or below this number, so a reorg can never invalidate an
    already-scored bar."""
    block = http_rpc(url, "eth_getBlockByNumber", ["finalized", False])
    return int(block["number"], 16)


def get_latest_block(url: str) -> int:
    return int(http_rpc(url, "eth_blockNumber", []), 16)


def _etherscan_page(
    address: str,
    topic0: str,
    from_block: int,
    to_block: int,
    page: int,
    api_key: str,
    retries: int = 5,
) -> list[dict] | None:
    params = {
        "chainid": 1,
        "module": "logs",
        "action": "getLogs",
        "address": address,
        "topic0": topic0,
        "fromBlock": from_block,
        "toBlock": to_block,
        "page": page,
        "offset": _ETHERSCAN_PAGE_SIZE,
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
        time.sleep(2.0 * (attempt + 1))
    return None


def _etherscan_get_logs_window(
    address: str, topic0: str, from_block: int, to_block: int, api_key: str
) -> list[dict]:
    logs: list[dict] = []
    page = 1
    while True:
        batch = _etherscan_page(address, topic0, from_block, to_block, page, api_key)
        if batch is None:
            raise RuntimeError(
                f"Etherscan getLogs failed for [{from_block},{to_block}] page {page}"
            )
        logs.extend(batch)
        if len(batch) < _ETHERSCAN_PAGE_SIZE:
            return logs
        if page * _ETHERSCAN_PAGE_SIZE >= _ETHERSCAN_RESULT_WINDOW_CAP:
            # Hit the window cap -- this range has more than 10k matching
            # logs; bisect instead of silently truncating.
            return logs
        page += 1


def etherscan_get_logs_paginated(
    address: str, topic0: str, from_block: int, to_block: int, api_key: str
) -> list[dict]:
    """Every matching log in [from_block, to_block], recursively bisecting
    whenever a window returns exactly the 10,000-row cap."""
    logs = _etherscan_get_logs_window(address, topic0, from_block, to_block, api_key)
    if len(logs) < _ETHERSCAN_RESULT_WINDOW_CAP or from_block >= to_block:
        return logs
    mid = (from_block + to_block) // 2
    left = etherscan_get_logs_paginated(address, topic0, from_block, mid, api_key)
    right = etherscan_get_logs_paginated(address, topic0, mid + 1, to_block, api_key)
    return left + right


async def ws_subscribe_logs(
    ws_url: str, address: str, topic0: str
) -> AsyncIterator[dict]:
    """Yield each `eth_subscription` log push for (address, topic0) forever.
    Reconnects with backoff on any connection drop -- the caller sees a
    single unbroken async stream and doesn't need to know a reconnect
    happened."""
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(ws_url, open_timeout=15) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "eth_subscribe",
                            "params": [
                                "logs",
                                {"address": address, "topics": [topic0]},
                            ],
                        }
                    )
                )
                sub_ack = json.loads(await ws.recv())
                if "result" not in sub_ack:
                    raise RuntimeError(f"eth_subscribe rejected: {sub_ack}")
                backoff = 1.0  # reset after a clean (re)connect
                async for raw in ws:
                    msg = json.loads(raw)
                    params = msg.get("params")
                    if params and "result" in params:
                        yield params["result"]
        except (websockets.exceptions.WebSocketException, OSError, TimeoutError):
            await _sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
