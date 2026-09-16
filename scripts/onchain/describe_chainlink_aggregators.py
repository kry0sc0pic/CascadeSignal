"""Describe every Chainlink aggregator seen in the pulled `AnswerUpdated` data.

`scripts/onchain/fetch_aave_v2_price_oracle_sources.py` established that
Aave v2's *current* `getSourceOfAsset()` mapping points at modern "Capped
X/USD/ETH" wrapper contracts -- none of which appear anywhere in the 423,848
`AnswerUpdated` rows already pulled for the 5 golden episodes (they simply
didn't exist yet during China'21/Terra'22/FTX'22, and Oct'25/Feb'26 volume
for them was apparently still nil at pull time). That current-state mapping
is a dead end for historical price reconstruction -- same class of problem as
`reserves.py`'s frozen/de-risked liquidation thresholds.

Instead of relying on Aave's oracle pointer, this script identifies the raw
Chainlink feeds directly: for each of the 392 aggregator addresses that
actually emitted events in our pulled data, fetch `description()` and
`decimals()` (feed identity metadata, stable over a feed's lifetime, unlike
Aave's swappable oracle-source pointer) via free public JSON-RPC. The output
lets `map_chainlink_reserves.py`-style symbol matching find e.g. "DAI / USD"
or "LINK / ETH" among aggregators that were actually live and pulled for the
golden-episode block windows -- no Dune credits spent.

Usage:
    python scripts/onchain/describe_chainlink_aggregators.py \\
        data/raw/chainlink/chain=1/*.parquet
Prints a JSON dict (aggregator address -> {decimals, description, rows}) to
stdout.
"""

from __future__ import annotations

import glob
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pandas as pd
import requests

RPC_URL = "https://ethereum-rpc.publicnode.com"

# Same selectors as fetch_aave_v2_price_oracle_sources.py -- both are
# well-known standard ERC20/Chainlink AggregatorV3Interface selectors,
# cross-verified there via pycryptodome keccak256.
SELECTOR_DECIMALS = "313ce567"
SELECTOR_DESCRIPTION = "7284e416"

_MAX_WORKERS = 8


def eth_call(to: str, data: str, retries: int = 4) -> str | None:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
        "id": 1,
    }
    for attempt in range(retries):
        try:
            resp = requests.post(RPC_URL, json=payload, timeout=20)
            result = resp.json()
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
            continue
        if "error" in result:
            time.sleep(1.5 * (attempt + 1))
            continue
        return result["result"]
    return None


def decode_uint(hex_data: str) -> int:
    return int(hex_data, 16) if hex_data != "0x" else 0


def decode_string(hex_data: str) -> str:
    data = bytes.fromhex(hex_data[2:])
    if len(data) < 64:
        return ""
    str_offset = int.from_bytes(data[0:32], "big")
    str_len = int.from_bytes(data[str_offset : str_offset + 32], "big")
    raw = data[str_offset + 32 : str_offset + 32 + str_len]
    return raw.decode("utf-8", errors="replace")


def describe(address: str) -> dict[str, Any]:
    decimals_hex = eth_call(address, "0x" + SELECTOR_DECIMALS)
    description_hex = eth_call(address, "0x" + SELECTOR_DESCRIPTION)
    if decimals_hex is None or description_hex is None:
        return {"decimals": None, "description": None, "note": "eth_call failed"}
    return {
        "decimals": decode_uint(decimals_hex),
        "description": decode_string(description_hex),
    }


def main() -> None:
    patterns = sys.argv[1:] or ["data/raw/chainlink/chain=1/*.parquet"]
    paths: list[str] = []
    for pattern in patterns:
        paths.extend(glob.glob(pattern))
    df = pd.concat([pd.read_parquet(p) for p in sorted(paths)], ignore_index=True)
    row_counts: dict[str, int] = {
        str(k): int(v) for k, v in df.groupby("user").size().to_dict().items()
    }
    addresses = sorted(row_counts)

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(describe, addr): addr for addr in addresses}
        for future in as_completed(futures):
            addr = futures[future]
            info = future.result()
            info["rows"] = int(row_counts[addr])
            out[addr] = info

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
