"""Tests for the Aave v2 aToken-transfer correction puller (Track C).

Covers the offline, no-network pieces of
`scripts/onchain/backfill_atoken_transfers.py`:

- `_decode`: a raw Etherscan `Transfer` log becomes a (block_number,
 log_index, tx_hash, reserve, from_user, to_user, value_raw) row, reading
 `from`/`to` from `topics[1]`/`topics[2]` and `value` from `data` (the only
 non-indexed field) -- see module docstring for the on-chain verification
 this is based on.
- Mint (`from == 0x0`) and burn (`to == 0x0`) legs are dropped: they're
 already captured by `Deposit`/`Withdraw`, only "naked" (both-legs-nonzero)
 transfers represent collateral movement this engine doesn't otherwise see.

The live pull (`_pull_atoken` / `main`) is network-bound (Etherscan) and not
exercised here -- same convention as `test_backfill_collateral_toggle.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve.parent.parent / "scripts" / "onchain"))

import backfill_atoken_transfers as bat # noqa: E402

ZERO_TOPIC = "0x" + "0" * 64


def _transfer_log(
 block_number: int,
 log_index: int,
 from_addr: str,
 to_addr: str,
 value: int,
 tx_hash: str = "0xtx",
) -> dict:
 return {
 "blockNumber": hex(block_number),
 "logIndex": hex(log_index),
 "transactionHash": tx_hash,
 "topics": ["0xtopic0", from_addr, to_addr],
 "data": hex(value),
 }


# ---------------------------------------------------------------------------
# _hex_to_int
# ---------------------------------------------------------------------------


def test_hex_to_int_treats_bare_0x_as_zero:
 assert bat._hex_to_int("0x") == 0
 assert bat._hex_to_int("0x0") == 0
 assert bat._hex_to_int("0x8e") == 142


# ---------------------------------------------------------------------------
# _decode
# ---------------------------------------------------------------------------


def test_decode_reads_from_to_value_from_a_naked_transfer:
 from_topic = "0x000000000000000000000000" + "1" * 40
 to_topic = "0x000000000000000000000000" + "2" * 40
 log = _transfer_log(11_500_123, 7, from_topic, to_topic, 5 * 10**18, "0xabc")
 (row,) = bat._decode("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", [log])
 assert row == {
 "block_number": 11_500_123,
 "log_index": 7,
 "tx_hash": "0xabc",
 "reserve": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
 "from_user": "0x" + "1" * 40,
 "to_user": "0x" + "2" * 40,
 "value_raw": str(5 * 10**18),
 }


def test_decode_drops_mint_legs:
 to_topic = "0x000000000000000000000000" + "2" * 40
 log = _transfer_log(100, 0, ZERO_TOPIC, to_topic, 10**18)
 assert bat._decode("0xreserve", [log]) == []


def test_decode_drops_burn_legs:
 from_topic = "0x000000000000000000000000" + "1" * 40
 log = _transfer_log(100, 0, from_topic, ZERO_TOPIC, 10**18)
 assert bat._decode("0xreserve", [log]) == []


def test_decode_keeps_naked_transfers_among_mixed_logs:
 from_topic = "0x000000000000000000000000" + "1" * 40
 to_topic = "0x000000000000000000000000" + "2" * 40
 logs = [
 _transfer_log(100, 0, ZERO_TOPIC, to_topic, 10**18), # mint, dropped
 _transfer_log(101, 0, from_topic, to_topic, 2 * 10**18), # naked, kept
 _transfer_log(102, 0, from_topic, ZERO_TOPIC, 10**18), # burn, dropped
 ]
 rows = bat._decode("0xreserve", logs)
 assert len(rows) == 1
 assert rows[0]["block_number"] == 101
 assert rows[0]["value_raw"] == str(2 * 10**18)
