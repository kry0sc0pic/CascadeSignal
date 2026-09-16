"""Tests for the Aave v2 Deposit/Borrow `onBehalfOf` correction puller (CAS-28).

Covers the offline, no-network pieces of
`scripts/onchain/fix_gateway_onbehalfof.py`:

- `_decode`: a raw Etherscan log becomes a (tx_hash, log_index, onbehalfof)
  correction row, reading `onBehalfOf` from `topics[2]` (not the `user`
  field in `data`, which is msg.sender -- see module docstring for the
  on-chain verification this is based on).
- `_chunks`: the block-range chunking used for checkpointing covers
  `[_MIN_BLOCK, _MAX_BLOCK]` exactly once, with no gaps or overlaps.

The live pull (`_pull_chunk` / `main`) is network-bound (Etherscan) and not
exercised here -- same convention as `fetch_svr_feed_events` /
`backfill_chainlink_t2_windows` having no network test.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "onchain"))

import fix_gateway_onbehalfof as fx  # noqa: E402

# ---------------------------------------------------------------------------
# _decode
# ---------------------------------------------------------------------------


def test_decode_reads_onbehalfof_from_topic2_not_data():
    log = {
        "transactionHash": "0xCAF54B",
        "logIndex": hex(315),
        "topics": [
            "0xtopic0",
            "0x000000000000000000000000c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # reserve
            "0x00000000000000000000000056618ca46b82a1309f15aa3ec5dfc894756dc069",  # onBehalfOf
            "0x0000000000000000000000000000000000000000000000000000000000000000",  # referral
        ],
        "data": "0x000000000000000000000000cc9a0b7c43dc2a5f023bb9b738e45b0ef6b06e04"
        "0000000000000000000000000000000000000000000000000de0b6b3a7640000",
    }
    (row,) = fx._decode("Deposit", [log])
    assert row == {
        "tx_hash": "0xcaf54b",
        "log_index": 315,
        "event_type": "Deposit",
        "onbehalfof": "0x56618ca46b82a1309f15aa3ec5dfc894756dc069",
    }


def test_decode_multiple_logs_preserves_order():
    logs = [
        {
            "transactionHash": "0xa",
            "logIndex": hex(1),
            "topics": ["0xt0", "0xreserve", "0x" + "1" * 40, "0x0"],
            "data": "0x" + "0" * 64,
        },
        {
            "transactionHash": "0xb",
            "logIndex": hex(2),
            "topics": ["0xt0", "0xreserve", "0x" + "2" * 40, "0x0"],
            "data": "0x" + "0" * 64,
        },
    ]
    rows = fx._decode("Borrow", logs)
    assert [r["tx_hash"] for r in rows] == ["0xa", "0xb"]
    assert rows[0]["onbehalfof"] == "0x" + "1" * 40
    assert rows[1]["onbehalfof"] == "0x" + "2" * 40
    assert all(r["event_type"] == "Borrow" for r in rows)


# ---------------------------------------------------------------------------
# _chunks
# ---------------------------------------------------------------------------


def test_chunks_cover_full_range_without_gaps_or_overlaps():
    chunks = fx._chunks()
    assert chunks[0][0] == fx._MIN_BLOCK
    assert chunks[-1][1] == fx._MAX_BLOCK - 1

    for (_, hi), (next_lo, _) in zip(chunks, chunks[1:]):
        assert next_lo == hi + 1  # contiguous, no gap or overlap

    for lo, hi in chunks:
        assert lo <= hi
