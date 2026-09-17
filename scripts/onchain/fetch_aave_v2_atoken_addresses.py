"""Pull each Aave v2 Ethereum reserve's aToken/debt-token addresses.

Originally just the aToken address, needed to pull aToken `Transfer` logs for
the Deposit/Withdraw gateway-asymmetry fix (see `engine.py`'s module
docstring): a gateway-routed `Withdraw` (e.g. `WETHGateway.withdrawETH`)
first pulls the real user's aTokens into the gateway via
`aToken.transferFrom(realUser, gateway, amount)` *before* calling
`LendingPool.withdraw` as itself -- so the aToken `Transfer` event
immediately preceding the `Withdraw` log in the same tx names the real user
as its `from`. Correlating that requires knowing which ERC20 contract
address is the aToken for each underlying reserve; Aave v2 doesn't derive
this deterministically (no CREATE2 salt convention), so it has to be read
on-chain via `AaveProtocolDataProvider.getReserveTokensAddresses(asset)`,
the same contract + RPC pattern `fetch_aave_v2_reserve_config.py` already
uses for risk params.

Extended to also emit the stableDebtToken/variableDebtToken
addresses that same call already returns (previously decoded then discarded)
-- needed to pull each debt token's own `Mint`/`Burn` events for the exact
token-level ledger (see `state/engine.py`'s H3 section).

Usage:
 python scripts/onchain/fetch_aave_v2_atoken_addresses.py
Prints three Python dict literals (reserve address -> token address) to
stdout, suitable for pasting into `reserves.py`'s `ATOKEN_ADDRESS_BY_RESERVE`,
`STABLE_DEBT_TOKEN_ADDRESS_BY_RESERVE`, `VARIABLE_DEBT_TOKEN_ADDRESS_BY_RESERVE`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve.parent))
sys.path.insert(0, str(Path(__file__).resolve.parent.parent.parent / "src"))

from fetch_aave_v2_reserve_config import DATA_PROVIDER, eth_call, get_block_number

from cascadesignal.state.reserves import ONCHAIN_RESERVE_CONFIG

# keccak256("getReserveTokensAddresses(address)"), derived once via
# pycryptodome's Crypto.Hash.keccak (not memorized) -- same derivation
# approach `fetch_aave_v2_reserve_config.py` uses for its own selectors.
SELECTOR_RESERVE_TOKENS = "d2493b6c"


def decode_reserve_tokens(hex_data: str) -> dict[str, str]:
 data = bytes.fromhex(hex_data[2:])
 words = [data[i : i + 32] for i in range(0, len(data), 32)]
 a_token, stable_debt, variable_debt = (w[12:].hex for w in words[0:3])
 return {
 "a_token": "0x" + a_token,
 "stable_debt_token": "0x" + stable_debt,
 "variable_debt_token": "0x" + variable_debt,
 }


def main -> None:
 block_number = get_block_number
 block_hex = hex(block_number)

 a_token: dict[str, str] = {}
 stable_debt: dict[str, str] = {}
 variable_debt: dict[str, str] = {}
 for address in sorted(ONCHAIN_RESERVE_CONFIG):
 arg = address[2:].rjust(64, "0").lower
 calldata = "0x" + SELECTOR_RESERVE_TOKENS + arg
 tokens = decode_reserve_tokens(
 eth_call(DATA_PROVIDER, calldata, block=block_hex)
 )
 a_token[address] = tokens["a_token"].lower
 stable_debt[address] = tokens["stable_debt_token"].lower
 variable_debt[address] = tokens["variable_debt_token"].lower

 print(f"# Snapshot pinned to block {block_number} ({len(a_token)} reserves)")
 print("# ATOKEN_ADDRESS_BY_RESERVE")
 print(json.dumps(a_token, indent=2))
 print("# STABLE_DEBT_TOKEN_ADDRESS_BY_RESERVE")
 print(json.dumps(stable_debt, indent=2))
 print("# VARIABLE_DEBT_TOKEN_ADDRESS_BY_RESERVE")
 print(json.dumps(variable_debt, indent=2))


if __name__ == "__main__":
 main
