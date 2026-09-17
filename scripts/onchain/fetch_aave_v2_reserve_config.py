"""Pull Aave v2 Ethereum's live on-chain reserve list + risk config.

Replaces guessed/memorized reserve parameters in `cascadesignal.state.reserves`
with real values read directly from the `AaveProtocolDataProvider` contract via
a public JSON-RPC endpoint -- no Dune credits needed for this piece (Dune is
still required for the historical `ReserveDataUpdated` event stream / interest
accrual, which stays blocked until credits reset 2026-08-03).

IMPORTANT: this reads *current* on-chain state only. Aave v2 Ethereum is now
fully frozen (every reserve's `isFrozen` flag is true) as part of its
deprecation in favor of v3, and most reserves were de-risked toward a
liquidation threshold of ~0 well before the freeze. These are NOT the risk
parameters that were active during the 2021-2022 golden episodes (China,
Terra) -- do not use this snapshot for historical HF reconstruction without a
governance-history pull. See `reserves.py`'s module docstring.

Usage:
 python scripts/onchain/fetch_aave_v2_reserve_config.py
Prints a Python dict literal (address -> config) to stdout, suitable for
pasting into `reserves.py`'s ONCHAIN_RESERVE_CONFIG, plus the block number
the snapshot was pinned to.
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

RPC_URL = "https://ethereum-rpc.publicnode.com"
# AaveProtocolDataProvider (Aave v2 Ethereum), verified against
# github.com/bgd-labs/aave-address-book/blob/main/src/AaveV2Ethereum.sol
DATA_PROVIDER = "0x057835Ad21a177dbdd3090bB1CAE03EaCF78Fc6d"

# 4-byte selectors = first 4 bytes of keccak256(signature). Derived once via
# `Crypto.Hash.keccak` (pycryptodome), not memorized:
# keccak256("getAllReservesTokens") -> 0xb316ff89
# keccak256("getReserveConfigurationData(address)") -> 0x3e150141
SELECTOR_ALL_RESERVES = "b316ff89"
SELECTOR_RESERVE_CONFIG = "3e150141"


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


def decode_all_reserves_tokens(hex_data: str) -> list[tuple[str, str]]:
 data = bytes.fromhex(hex_data[2:])
 array_offset = int.from_bytes(data[0:32], "big")
 array_len = int.from_bytes(data[array_offset : array_offset + 32], "big")
 tuples_start = array_offset + 32
 out = []
 for i in range(array_len):
 tuple_offset_rel = int.from_bytes(
 data[tuples_start + i * 32 : tuples_start + i * 32 + 32], "big"
 )
 tuple_abs = tuples_start + tuple_offset_rel
 str_offset_rel = int.from_bytes(data[tuple_abs : tuple_abs + 32], "big")
 token_address = data[tuple_abs + 32 + 12 : tuple_abs + 32 + 32].hex
 str_abs = tuple_abs + str_offset_rel
 str_len = int.from_bytes(data[str_abs : str_abs + 32], "big")
 symbol = data[str_abs + 32 : str_abs + 32 + str_len].decode(
 "utf-8", errors="replace"
 )
 out.append((symbol, "0x" + token_address))
 return out


def decode_reserve_config(hex_data: str) -> dict:
 data = bytes.fromhex(hex_data[2:])
 words = [int.from_bytes(data[i : i + 32], "big") for i in range(0, len(data), 32)]
 decimals, ltv, liq_threshold, liq_bonus, reserve_factor = words[0:5]
 usage_as_collateral, borrowing_enabled, stable_borrow, is_active, is_frozen = [
 bool(w) for w in words[5:10]
 ]
 return {
 "decimals": decimals,
 "ltv_bps": ltv,
 "liquidation_threshold_bps": liq_threshold,
 "liquidation_bonus_bps": liq_bonus,
 "reserve_factor_bps": reserve_factor,
 "usage_as_collateral_enabled": usage_as_collateral,
 "borrowing_enabled": borrowing_enabled,
 "stable_borrow_rate_enabled": stable_borrow,
 "is_active": is_active,
 "is_frozen": is_frozen,
 }


def main -> None:
 block_number = get_block_number
 block_hex = hex(block_number)
 reserves = decode_all_reserves_tokens(
 eth_call(DATA_PROVIDER, "0x" + SELECTOR_ALL_RESERVES, block=block_hex)
 )

 out = {}
 for symbol, address in reserves:
 arg = address[2:].rjust(64, "0").lower
 calldata = "0x" + SELECTOR_RESERVE_CONFIG + arg
 cfg = decode_reserve_config(eth_call(DATA_PROVIDER, calldata, block=block_hex))
 cfg["symbol"] = symbol
 out[address.lower] = cfg

 print(f"# Snapshot pinned to block {block_number} ({len(out)} reserves)")
 print(json.dumps(out, indent=2))


if __name__ == "__main__":
 main
