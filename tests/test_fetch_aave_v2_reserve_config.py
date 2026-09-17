"""Unit tests for the ABI-decoding logic in
scripts/onchain/fetch_aave_v2_reserve_config.py.

This script has no automated test in `uv run pytest` precedent for sibling
ingesters (`defillama_prices.py`, `dune.py` have none either -- they need a
live network call to exercise meaningfully). But its ABI decoders are pure
functions operating on raw hex, and a subtly wrong decode would silently
corrupt reserve config data with no error -- exactly the kind of logic worth
testing without a network call. Fixtures below are either hand-constructed
per the ABI spec (ltv/lt/bonus one) or captured verbatim from a real
`eth_call` response (config one) and cross-checked against
`state/reserves.py`'s independently-verified WETH values.

`scripts/` isn't an importable package (no `__init__.py`, self-contained
scripts), so this loads the module directly by file path rather than
`import scripts.onchain....`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT_PATH = (
 Path(__file__).parent.parent
 / "scripts"
 / "onchain"
 / "fetch_aave_v2_reserve_config.py"
)
_spec = importlib.util.spec_from_file_location(
 "fetch_aave_v2_reserve_config", _SCRIPT_PATH
)
assert _spec is not None and _spec.loader is not None
fetch_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_config)

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

# Hand-constructed per the ABI spec for `(string,address)[]` -- NOT produced
# by round-tripping through decode_all_reserves_tokens itself, to avoid a
# self-consistent blind spot.
_TWO_RESERVE_TOKENS_HEX = (
 "0x"
 "0000000000000000000000000000000000000000000000000000000000000020"
 "0000000000000000000000000000000000000000000000000000000000000002"
 "0000000000000000000000000000000000000000000000000000000000000040"
 "00000000000000000000000000000000000000000000000000000000000000c0"
 "0000000000000000000000000000000000000000000000000000000000000040"
 f"000000000000000000000000{WETH[2:]}"
 "0000000000000000000000000000000000000000000000000000000000000004"
 "5745544800000000000000000000000000000000000000000000000000000000"
 "0000000000000000000000000000000000000000000000000000000000000040"
 f"000000000000000000000000{USDC[2:]}"
 "0000000000000000000000000000000000000000000000000000000000000004"
 "5553444300000000000000000000000000000000000000000000000000000000"
)

# Captured verbatim from a real `eth_call` to AaveProtocolDataProvider's
# getReserveConfigurationData(WETH) at block 25479510 -- matches
# state/reserves.py's ONCHAIN_RESERVE_CONFIG["...c02aaa..."] WETH row
# (18 decimals, 0.825 ltv, 0.86 liquidation_threshold, 0.05 bonus, frozen).
_WETH_CONFIG_HEX = (
 "0x000000000000000000000000000000000000000000000000000000000000001200000000000000"
 "0000000000000000000000000000000000000000000000203a000000000000000000000000000000"
 "00000000000000000000000000000021980000000000000000000000000000000000000000000000"
 "00000000000000290400000000000000000000000000000000000000000000000000000000000021"
 "34000000000000000000000000000000000000000000000000000000000000000100000000000000"
 "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
 "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
 "00000000000000000100000000000000000000000000000000000000000000000000000000000000"
 "01"
)


def test_decode_all_reserves_tokens_parses_hand_built_abi_encoding:
 result = fetch_config.decode_all_reserves_tokens(_TWO_RESERVE_TOKENS_HEX)
 assert result == [("WETH", WETH), ("USDC", USDC)]


def test_decode_reserve_config_matches_known_verified_weth_values:
 cfg = fetch_config.decode_reserve_config(_WETH_CONFIG_HEX)
 assert cfg["decimals"] == 18
 assert cfg["ltv_bps"] == 8250 # 0.825, matches reserves.py's verified WETH ltv
 assert cfg["liquidation_threshold_bps"] == 8600 # 0.86
 assert cfg["liquidation_bonus_bps"] == 10500 # 1.05 -> 0.05 bonus
 assert cfg["usage_as_collateral_enabled"] is True
 assert cfg["borrowing_enabled"] is False
 assert cfg["is_active"] is True
 assert cfg["is_frozen"] is True
