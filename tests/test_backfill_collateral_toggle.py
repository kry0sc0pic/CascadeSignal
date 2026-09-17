"""Tests for the Aave v2 collateral-toggle correction puller (Track B).

Covers the offline, no-network pieces of
`scripts/onchain/backfill_collateral_toggle.py`:

- `_decode`: a raw Etherscan log becomes a (block_number, log_index, reserve,
 user, enabled) row, reading `reserve` from `topics[1]` and `user` from
 `topics[2]` -- both indexed, no `data` payload (see module docstring for
 the on-chain verification this is based on).
- `_chunks`: the block-range chunking used for checkpointing covers
 `[_MIN_BLOCK, _MAX_BLOCK]` exactly once, with no gaps or overlaps.

The live pull (`_pull_chunk` / `main`) is network-bound (Etherscan) and not
exercised here -- same convention as `fix_gateway_onbehalfof`'s test.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve.parent.parent / "scripts" / "onchain"))

import backfill_collateral_toggle as bct # noqa: E402

# ---------------------------------------------------------------------------
# _hex_to_int
# ---------------------------------------------------------------------------


def test_hex_to_int_treats_bare_0x_as_zero:
 # Observed for real: Etherscan returns a bare "0x" (not "0x0") for
 # `logIndex` when `ReserveUsedAsCollateralEnabled` is the first log in a
 # tx (emitted before `Deposit` on a user's first-ever deposit of a
 # reserve) -- `int("0x", 16)` raises, so this must be normalized.
 assert bct._hex_to_int("0x") == 0
 assert bct._hex_to_int("0x0") == 0
 assert bct._hex_to_int("0x8e") == 142


# ---------------------------------------------------------------------------
# _decode
# ---------------------------------------------------------------------------


def test_decode_reads_reserve_and_user_from_topics_not_data:
 log = {
 "blockNumber": hex(11_500_123),
 "logIndex": hex(7),
 "topics": [
 "0xtopic0",
 "0x000000000000000000000000c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", # reserve
 "0x00000000000000000000000056618ca46b82a1309f15aa3ec5dfc894756dc069", # user
 ],
 "data": "0x",
 }
 (row,) = bct._decode(True, [log])
 assert row == {
 "block_number": 11_500_123,
 "log_index": 7,
 "reserve": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
 "user": "0x56618ca46b82a1309f15aa3ec5dfc894756dc069",
 "enabled": True,
 }


def test_decode_multiple_logs_preserves_order_and_enabled_flag:
 logs = [
 {
 "blockNumber": hex(100),
 "logIndex": hex(1),
 "topics": ["0xt0", "0x" + "a" * 40, "0x" + "1" * 40],
 "data": "0x",
 },
 {
 "blockNumber": hex(200),
 "logIndex": hex(2),
 "topics": ["0xt0", "0x" + "b" * 40, "0x" + "2" * 40],
 "data": "0x",
 },
 ]
 rows = bct._decode(False, logs)
 assert [r["block_number"] for r in rows] == [100, 200]
 assert rows[0]["reserve"] == "0x" + "a" * 40
 assert rows[0]["user"] == "0x" + "1" * 40
 assert all(r["enabled"] is False for r in rows)


# ---------------------------------------------------------------------------
# _chunks
# ---------------------------------------------------------------------------


def test_chunks_cover_full_range_without_gaps_or_overlaps:
 chunks = bct._chunks
 assert chunks[0][0] == bct._MIN_BLOCK
 assert chunks[-1][1] == bct._MAX_BLOCK - 1

 for (_, hi), (next_lo, _) in zip(chunks, chunks[1:]):
 assert next_lo == hi + 1 # contiguous, no gap or overlap

 for lo, hi in chunks:
 assert lo <= hi
