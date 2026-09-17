"""Map Aave v2 Ethereum reserves to their Chainlink aggregator sources.

Building a block-level price timeline out of the Chainlink `AnswerUpdated`
events already pulled via Dune (`data/raw/chainlink/chain=1/`) requires
knowing *which* aggregator contract prices *which* reserve. This script reads
that mapping directly from Aave v2's own on-chain `AaveOracle` contract via
`getSourceOfAsset(reserve)` -- no Dune credits needed, and no reliance on a
copy-pasted address list (a prior scrape of the aave-address-book file via a
markdown-rendering tool silently corrupted several addresses with spurious
extra hex digits; raw `curl` + this on-chain cross-check avoided shipping
that corruption into the repo).

`ORACLE` below (0xA50ba011c48153De246E5192C8f9258A2ba79Ca9) is verified two
ways: (1) it matches `AaveV2Ethereum.sol` in github.com/bgd-labs/aave-address-book
fetched via plain `curl` (not a markdown scraper), and (2) calling
`getPriceOracle` on `POOL_ADDRESSES_PROVIDER`
(0xB53C1a33016B2DC2fF3653530bfF1848a515c8c5, also from that file) returns the
same address live on-chain.

IMPORTANT gotcha this script exists to resolve: Aave v2's oracle does not
uniformly use USD-denominated Chainlink feeds. Some reserves are priced via
ETH-denominated feeds (18 decimals) rather than USD-denominated ones (8
decimals). `scripts/dune/chainlink_prices.sql` unconditionally computed
`amount_usd = current / 1e8` for every aggregator pulled -- that column is
WRONG for any ETH-denominated feed and must be ignored in favor of
recomputing from `amount_raw` using the real per-aggregator `decimals`
fetched here (and, for ETH-denominated feeds, converting via the ETH/USD
feed). `description` is also fetched so the ETH-vs-USD split doesn't rely
on decimals alone (some feeds may use non-standard decimals for other
reasons).

Usage:
 python scripts/onchain/fetch_aave_v2_price_oracle_sources.py
Prints a JSON dict (reserve address -> {aggregator, decimals, description})
to stdout, plus the block number the snapshot was pinned to.
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

from cascadesignal.state.reserves import ONCHAIN_RESERVE_CONFIG

RPC_URL = "https://ethereum-rpc.publicnode.com"

# Verified on-chain (see module docstring): getPriceOracle on
# POOL_ADDRESSES_PROVIDER returns this address live.
POOL_ADDRESSES_PROVIDER = "0xB53C1a33016B2DC2fF3653530bfF1848a515c8c5"
ORACLE = "0xA50ba011c48153De246E5192C8f9258A2ba79Ca9"

# 4-byte selectors, derived via `Crypto.Hash.keccak` (pycryptodome), not
# memorized -- decimals/description also cross-check against the
# well-known standard ERC20/Chainlink selector values:
# keccak256("getPriceOracle") -> 0xfca513a8
# keccak256("getSourceOfAsset(address)") -> 0x92bf2be0
# keccak256("decimals") -> 0x313ce567
# keccak256("description") -> 0x7284e416
SELECTOR_GET_PRICE_ORACLE = "fca513a8"
SELECTOR_GET_SOURCE_OF_ASSET = "92bf2be0"
SELECTOR_DECIMALS = "313ce567"
SELECTOR_DESCRIPTION = "7284e416"

ZERO_ADDRESS = "0x" + "00" * 20


def eth_call(to: str, data: str, block: str = "latest", retries: int = 4) -> str:
 payload: dict[str, Any] = {
 "jsonrpc": "2.0",
 "method": "eth_call",
 "params": [{"to": to, "data": data}, block],
 "id": 1,
 }
 result: dict = {}
 for attempt in range(retries):
 resp = requests.post(RPC_URL, json=payload, timeout=20)
 result = resp.json
 if "error" in result:
 time.sleep(1.5 * (attempt + 1))
 continue
 return result["result"]
 raise RuntimeError(f"eth_call failed after {retries} retries: {result}")


def get_block_number -> int:
 payload: dict[str, Any] = {
 "jsonrpc": "2.0",
 "method": "eth_blockNumber",
 "params": [],
 "id": 1,
 }
 resp = requests.post(RPC_URL, json=payload, timeout=20)
 return int(resp.json["result"], 16)


def decode_address(hex_data: str) -> str:
 return "0x" + hex_data[-40:]


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


def verify_oracle_address(block_hex: str) -> None:
 result = eth_call(
 POOL_ADDRESSES_PROVIDER, "0x" + SELECTOR_GET_PRICE_ORACLE, block=block_hex
 )
 live_oracle = decode_address(result).lower
 if live_oracle != ORACLE.lower:
 raise RuntimeError(
 f"getPriceOracle returned {live_oracle}, expected {ORACLE.lower} "
 "-- ORACLE constant is stale, re-derive before trusting this mapping."
 )


def main -> None:
 block_number = get_block_number
 block_hex = hex(block_number)
 verify_oracle_address(block_hex)

 out: dict[str, dict] = {}
 for address in ONCHAIN_RESERVE_CONFIG:
 symbol = ONCHAIN_RESERVE_CONFIG[address][0]
 arg = address[2:].rjust(64, "0").lower
 source_hex = eth_call(
 ORACLE, "0x" + SELECTOR_GET_SOURCE_OF_ASSET + arg, block=block_hex
 )
 aggregator = decode_address(source_hex).lower

 entry: dict[str, Any] = {"symbol": symbol, "aggregator": aggregator}
 if aggregator == ZERO_ADDRESS:
 entry["decimals"] = None
 entry["description"] = None
 entry["note"] = "no source registered (getSourceOfAsset returned 0x0)"
 else:
 try:
 decimals_hex = eth_call(
 aggregator, "0x" + SELECTOR_DECIMALS, block=block_hex
 )
 description_hex = eth_call(
 aggregator, "0x" + SELECTOR_DESCRIPTION, block=block_hex
 )
 entry["decimals"] = decode_uint(decimals_hex)
 entry["description"] = decode_string(description_hex)
 except RuntimeError as exc:
 # Some registered sources are not plain AggregatorV3Interface
 # contracts (e.g. a custom wrapper) and revert on
 # decimals/description -- record instead of crashing the
 # whole sweep, so one odd reserve doesn't block the other 36.
 entry["decimals"] = None
 entry["description"] = None
 entry["note"] = f"decimals/description call reverted: {exc}"
 out[address.lower] = entry

 print(f"# Snapshot pinned to block {block_number} ({len(out)} reserves)")
 print(json.dumps(out, indent=2))


if __name__ == "__main__":
 main
