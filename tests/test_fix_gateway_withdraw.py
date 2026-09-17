"""Tests for the Aave v2 Withdraw gateway-misattribution fix.

Covers the offline, no-network pieces of
`scripts/onchain/fix_gateway_withdraw.py`:

- `gateway_addresses`: derives the set of gateway/adapter contracts from
 the existing Deposit onBehalfOf corrections table (any `user` whose
 decoded `onbehalfof` differs).
- `build_withdraw_corrections`: correlates a gateway-attributed `Withdraw`
 with the same-tx aToken `Transfer` that funded it -- picking the closest
 *preceding* one, restricted to aToken addresses, ignoring transfers that
 land after the `Withdraw` log or belong to unrelated tokens/txs.

The live pull (`get_transfers_to` / `main`) is network-bound (Etherscan) and
not exercised here -- same convention as `fix_gateway_onbehalfof.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve.parent.parent / "scripts" / "onchain"))
sys.path.insert(0, str(Path(__file__).resolve.parent.parent / "src"))

import fix_gateway_withdraw as fw # noqa: E402

GATEWAY = "0xcc9a0b7c43dc2a5f023bb9b738e45b0ef6b06e04"
REAL_USER = "0x56618ca46b82a1309f15aa3ec5dfc894756dc069"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
A_WETH = "0x030ba81f1c18d280636f32af80b9aad02cf0854e" # ATOKEN_ADDRESS_BY_RESERVE[WETH]


def _transfer_log(
 tx_hash: str,
 log_index: int,
 address: str,
 frm: str,
 to: str,
 amount: int = 10**18,
) -> dict:
 return {
 "transactionHash": tx_hash,
 "logIndex": hex(log_index),
 "address": address,
 "topics": [
 "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
 "0x" + "0" * 24 + frm[2:],
 "0x" + "0" * 24 + to[2:],
 ],
 "data": hex(amount),
 }


# ---------------------------------------------------------------------------
# gateway_addresses
# ---------------------------------------------------------------------------


def test_gateway_addresses_returns_users_whose_onbehalfof_differs:
 events = pd.DataFrame(
 [
 {
 "tx_hash": "0xa",
 "log_index": 0,
 "event_type": "Deposit",
 "user": GATEWAY,
 },
 {
 "tx_hash": "0xb",
 "log_index": 0,
 "event_type": "Deposit",
 "user": REAL_USER,
 },
 ]
 )
 corrections = pd.DataFrame(
 [
 {
 "tx_hash": "0xa",
 "log_index": 0,
 "event_type": "Deposit",
 "onbehalfof": REAL_USER,
 },
 {
 "tx_hash": "0xb",
 "log_index": 0,
 "event_type": "Deposit",
 "onbehalfof": REAL_USER,
 },
 ]
 )
 assert fw.gateway_addresses(events, corrections) == [GATEWAY]


def test_gateway_addresses_ignores_borrow_rows:
 events = pd.DataFrame(
 [{"tx_hash": "0xa", "log_index": 0, "event_type": "Deposit", "user": GATEWAY}]
 )
 corrections = pd.DataFrame(
 [
 {
 "tx_hash": "0xa",
 "log_index": 0,
 "event_type": "Borrow",
 "onbehalfof": REAL_USER,
 },
 ]
 )
 assert fw.gateway_addresses(events, corrections) == []


# ---------------------------------------------------------------------------
# build_withdraw_corrections
# ---------------------------------------------------------------------------


_DEFAULT_AMOUNT = 10**18


def _withdraw_row(
 tx_hash: str, log_index: int, user: str, reserve: str, amount: int = _DEFAULT_AMOUNT
) -> dict:
 return {
 "tx_hash": tx_hash,
 "log_index": log_index,
 "user": user,
 "reserve": reserve,
 "amount_raw": str(amount),
 }


def test_correlates_preceding_atoken_transfer_to_gateway:
 withdraws = pd.DataFrame([_withdraw_row("0xtx1", 54, GATEWAY, WETH)])
 transfers = [_transfer_log("0xtx1", 46, A_WETH, REAL_USER, GATEWAY)]

 corrections = fw.build_withdraw_corrections(withdraws, transfers)
 assert len(corrections) == 1
 row = corrections.iloc[0]
 assert row["tx_hash"] == "0xtx1"
 assert row["log_index"] == 54
 assert row["real_user"] == REAL_USER


def test_ignores_non_atoken_transfers:
 # A raw-WETH transfer to the gateway (the underlying asset leg of the
 # withdraw, not the aToken pull) must not be mistaken for the real user.
 withdraws = pd.DataFrame([_withdraw_row("0xtx1", 54, GATEWAY, WETH)])
 transfers = [_transfer_log("0xtx1", 51, WETH, A_WETH, GATEWAY)]

 corrections = fw.build_withdraw_corrections(withdraws, transfers)
 assert corrections.empty


def test_ignores_transfers_after_the_withdraw_log:
 withdraws = pd.DataFrame([_withdraw_row("0xtx1", 10, GATEWAY, WETH)])
 transfers = [_transfer_log("0xtx1", 20, A_WETH, REAL_USER, GATEWAY)]

 corrections = fw.build_withdraw_corrections(withdraws, transfers)
 assert corrections.empty


def test_ignores_amount_mismatch:
 # Same tx/aToken/gateway/ordering, but a different amount -- this is the
 # exact shape of a false positive from an unrelated transfer bundled
 # into the same multi-step transaction (see module docstring: this
 # requirement was added after it caused a 28.1% -> 69.1% regression).
 withdraws = pd.DataFrame(
 [_withdraw_row("0xtx1", 54, GATEWAY, WETH, amount=_DEFAULT_AMOUNT)]
 )
 transfers = [
 _transfer_log(
 "0xtx1", 46, A_WETH, REAL_USER, GATEWAY, amount=_DEFAULT_AMOUNT * 2
 )
 ]

 corrections = fw.build_withdraw_corrections(withdraws, transfers)
 assert corrections.empty


def test_picks_closest_preceding_transfer_when_multiple_match:
 withdraws = pd.DataFrame([_withdraw_row("0xtx1", 54, GATEWAY, WETH)])
 other_user = "0x" + "9" * 40
 transfers = [
 _transfer_log("0xtx1", 20, A_WETH, other_user, GATEWAY),
 _transfer_log("0xtx1", 46, A_WETH, REAL_USER, GATEWAY),
 ]

 corrections = fw.build_withdraw_corrections(withdraws, transfers)
 assert len(corrections) == 1
 assert corrections.iloc[0]["real_user"] == REAL_USER


def test_ignores_transfers_from_a_different_tx:
 withdraws = pd.DataFrame([_withdraw_row("0xtx1", 54, GATEWAY, WETH)])
 transfers = [_transfer_log("0xtx_other", 10, A_WETH, REAL_USER, GATEWAY)]

 corrections = fw.build_withdraw_corrections(withdraws, transfers)
 assert corrections.empty


def test_returns_empty_frame_for_no_transfer_rows:
 withdraws = pd.DataFrame([_withdraw_row("0xtx1", 54, GATEWAY, WETH)])
 corrections = fw.build_withdraw_corrections(withdraws, [])
 assert corrections.empty
 assert list(corrections.columns) == ["tx_hash", "log_index", "real_user"]


# ---------------------------------------------------------------------------
# needed_pairs
# ---------------------------------------------------------------------------


def test_needed_pairs_reads_atoken_and_gateway_off_local_withdraws:
 withdraws = pd.DataFrame(
 [
 _withdraw_row("0xtx1", 54, GATEWAY, WETH),
 _withdraw_row("0xtx2", 10, GATEWAY, WETH), # duplicate pair
 ]
 )
 assert fw.needed_pairs(withdraws) == [(A_WETH, GATEWAY)]


def test_needed_pairs_drops_reserves_without_a_known_atoken:
 withdraws = pd.DataFrame([_withdraw_row("0xtx1", 54, GATEWAY, "0xnotareserve")])
 assert fw.needed_pairs(withdraws) == []
