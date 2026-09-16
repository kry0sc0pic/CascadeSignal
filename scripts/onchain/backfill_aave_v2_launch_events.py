"""Backfill Aave v2's missing launch-month core events via Etherscan (CAS-28).

Root cause found while tracing T2's negative-debt accounts: our ingested Aave
v2 core-event lake (`data/raw/aave_v2/`) starts at block 11,565,024, but Aave
v2 launched ~block 11,362,828 (Dec 2020). The original Dune pull began ~31
days late, so **the entire launch month of Deposit/Borrow/Repay/Withdraw/
LiquidationCall is missing** (the cutoff is uniform across all five event
types). Long-lived accounts that opened positions in that window carry their
early borrows forward, so our reconstruction sees their repays without the
matching borrows -- cumulative debt goes *negative* (provably: some accounts
reconstruct to less debt than the amount their liquidation actually repaid,
impossible under Aave's 50% close factor), inflating HF and manufacturing T2
mismatches. Confirmed on a sample account: 21 of its 124 on-chain USDC
borrows (1.06M USDC) fell in [11,390,645, 11,563,144] and were simply absent
from the raw parquet.

This pulls blocks [11,362,000, 11,565,023] for the `LendingPool`
(`0x7d2768de...`) -- all five event types -- via Etherscan (free, not
credit-metered), decodes them to the canonical schema, and writes them into
the *same* directory `load_events` globs. `load_events` de-dupes on
(tx_hash, log_index) and there's no block overlap, so this is purely
additive. Attribution: Borrow/Deposit are captured from the indexed
`onBehalfOf` topic directly (the real position holder), matching what
`load_events`'s onBehalfOf correction produces for the main lake -- and in
the launch month msg.sender == onBehalfOf anyway (the WETHGateway/adapter
contracts that motivate that correction did not exist yet).

Every event signature's `topic0` and topic/data layout was verified live
against a real Etherscan `getLogs` result in [11,362,000, 11,565,023] before
use (topic counts and data byte-lengths matched the ABI: Deposit 4 topics/64
data bytes, Borrow 4/128, Repay 4/32, Withdraw 4/32, LiquidationCall 4/128).

Usage:
    python scripts/onchain/backfill_aave_v2_launch_events.py
Writes `data/raw/aave_v2/chain=1/core_events_launch_backfill_11362000_11565023.parquet`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_svr_feed_events import get_logs_paginated  # noqa: E402

LENDING_POOL = "0x7d2768de32b0b80b7a3454c06bdac94a69ddc7a9"

# topic0 per event type -- all verified live, see module docstring.
TOPICS = {
    "Deposit": "0xde6857219544bb5b7746f48ed30be6386fefc61b2f864cacf559893bf50fd951",
    "Borrow": "0xc6a898309e823ee50bac64e45ca8adba6690e99e7841c45d754e2a38e9019d9b",
    "Repay": "0x4cdde6e09bb755c9a5589ebaec640bbfedff1362d4b255ebf8339782b9942faa",
    "Withdraw": "0x3115d1449a7b732c986cba18244e897a450f61e1bb8d589cd2e69e6c8924f9f7",
    "LiquidationCall": "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286",
}

_MIN_BLOCK = 11_362_000
# Pull *through* the main lake's first block (11,565,024) so the two files'
# block ranges overlap by one block rather than leaving a spurious gap over
# the quiet blocks 11,565,019-11,565,023 (no Aave v2 events there, but
# `gap_fill.audit_coverage` reads per-file min/max and can't tell "no events"
# from "missing data"). `load_events` de-dupes on (tx_hash, log_index), so the
# one overlapping block is harmless.
_MAX_BLOCK = 11_565_025
_CHUNK_SIZE = 50_000

_CHECKPOINT_DIR = Path("data/raw/.checkpoints")
_OUT_PARQUET = Path(
    "data/raw/aave_v2/chain=1/core_events_launch_backfill_11362000_11565024.parquet"
)

_SCHEMA_COLUMNS = [
    "chain_id",
    "block_number",
    "block_timestamp",
    "tx_hash",
    "log_index",
    "protocol",
    "event_type",
    "user",
    "collateral_asset",
    "debt_asset",
    "amount_raw",
    "amount_usd",
    "liquidator",
    "collateral_seized_raw",
    "collateral_seized_usd",
]


def _hex_to_int(value: str) -> int:
    return int(value, 16) if value not in ("0x", "", None) else 0


def _addr(topic_or_word: str) -> str:
    return ("0x" + topic_or_word[-40:]).lower()


def _words(data_hex: str) -> list[int]:
    data = bytes.fromhex(data_hex[2:])
    return [int.from_bytes(data[i : i + 32], "big") for i in range(0, len(data), 32)]


def _base(log: dict, event_type: str) -> dict:
    return {
        "chain_id": 1,
        "block_number": _hex_to_int(log["blockNumber"]),
        "block_timestamp": pd.Timestamp(
            _hex_to_int(log["timeStamp"]), unit="s", tz="UTC"
        ),
        "tx_hash": log["transactionHash"],
        "log_index": _hex_to_int(log["logIndex"]),
        "protocol": "aave_v2",
        "event_type": event_type,
        "user": None,
        "collateral_asset": None,
        "debt_asset": None,
        "amount_raw": None,
        "amount_usd": None,
        "liquidator": None,
        "collateral_seized_raw": None,
        "collateral_seized_usd": None,
    }


def _decode(logs: list[dict], event_type: str) -> list[dict]:
    """Decode one event type's logs into canonical-schema rows. The reserve of
    a core (non-liquidation) event lands in `debt_asset` -- the same schema
    quirk the engine relies on (`build_ledger`: `reserve = debt_asset`)."""
    rows = []
    for log in logs:
        topics = log["topics"]
        words = _words(log["data"])
        row = _base(log, event_type)
        if event_type in ("Deposit", "Borrow"):
            # topics: [sig, reserve, onBehalfOf, referral]; data: [user, amount, ...]
            row["debt_asset"] = _addr(topics[1])
            row["user"] = _addr(topics[2])  # onBehalfOf = real position holder
            row["amount_raw"] = str(words[1])
        elif event_type in ("Repay", "Withdraw"):
            # topics: [sig, reserve, user, repayer/to]; data: [amount]
            row["debt_asset"] = _addr(topics[1])
            row["user"] = _addr(topics[2])
            row["amount_raw"] = str(words[0])
        elif event_type == "LiquidationCall":
            # topics: [sig, collateralAsset, debtAsset, user]
            # data: [debtToCover, liquidatedCollateralAmount, liquidator, receiveAToken]
            row["collateral_asset"] = _addr(topics[1])
            row["debt_asset"] = _addr(topics[2])
            row["user"] = _addr(topics[3])
            row["amount_raw"] = str(words[0])
            row["collateral_seized_raw"] = str(words[1])
            row["liquidator"] = _addr(f"{words[2]:064x}")
        rows.append(row)
    return rows


def _enrich_usd(df: pd.DataFrame) -> None:
    """Populate `amount_usd` (and `collateral_seized_usd` for liquidations)
    from DefiLlama daily prices, in place -- the same source the main lake's
    USD columns come from, so `check_usd_sanity`'s null-fraction bar holds.
    Reserves without DefiLlama coverage stay null (a small tail)."""
    from cascadesignal.state.prices import PriceOracle
    from cascadesignal.state.reserves import reserve_table

    oracle = PriceOracle()
    decimals = reserve_table().set_index("address")["decimals"].to_dict()

    def _usd(amount_raw: object, reserve: object, ts: pd.Timestamp) -> float | None:
        if amount_raw is None or reserve is None:
            return None
        price = oracle.price_at(str(reserve), ts)
        if price is None:
            return None
        return float(str(amount_raw)) / 10 ** decimals.get(str(reserve), 18) * price

    df["amount_usd"] = [
        _usd(a, r, t)
        for a, r, t in zip(df["amount_raw"], df["debt_asset"], df["block_timestamp"])
    ]
    is_liq = df["event_type"] == "LiquidationCall"
    if is_liq.any():
        df.loc[is_liq, "collateral_seized_usd"] = [
            _usd(a, r, t)
            for a, r, t in zip(
                df.loc[is_liq, "collateral_seized_raw"],
                df.loc[is_liq, "collateral_asset"],
                df.loc[is_liq, "block_timestamp"],
            )
        ]


def _chunks() -> list[tuple[int, int]]:
    bounds = list(range(_MIN_BLOCK, _MAX_BLOCK, _CHUNK_SIZE)) + [_MAX_BLOCK]
    return [(lo, hi - 1) for lo, hi in zip(bounds[:-1], bounds[1:])]


def _pull_event(event_type: str, topic0: str, api_key: str) -> list[dict]:
    checkpoint = _CHECKPOINT_DIR / f"aave_v2_launch_{event_type}.json"
    if checkpoint.exists():
        rows: list[dict] = json.loads(checkpoint.read_text())
        print(f"  {event_type:16s}: {len(rows)} rows (checkpoint)", flush=True)
        return rows

    all_logs: list[dict] = []
    for lo, hi in _chunks():
        all_logs.extend(
            get_logs_paginated(
                LENDING_POOL, api_key, from_block=lo, to_block=hi, topic0=topic0
            )
        )
    rows = _decode(all_logs, event_type)
    _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(json.dumps(rows, default=str))
    print(f"  {event_type:16s}: {len(rows)} rows (checkpointed)", flush=True)
    return rows


def _pull_event_with_retry(
    event_type: str, topic0: str, api_key: str, attempts: int = 4
) -> list[dict]:
    for attempt in range(attempts):
        try:
            return _pull_event(event_type, topic0, api_key)
        except RuntimeError as exc:
            if attempt == attempts - 1:
                raise
            wait = 30 * (attempt + 1)
            print(f"  retry {event_type} in {wait}s after: {exc}", flush=True)
            time.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def main() -> None:
    api_key = os.environ["ETHERSCAN_API_KEY"]
    print(
        f"Backfilling Aave v2 launch-month core events [{_MIN_BLOCK}, {_MAX_BLOCK}]...",
        flush=True,
    )
    all_rows: list[dict] = []
    for event_type, topic0 in TOPICS.items():
        all_rows.extend(_pull_event_with_retry(event_type, topic0, api_key))

    if not all_rows:
        raise RuntimeError("No launch-month events pulled -- nothing to write")

    df = pd.DataFrame(all_rows)
    df = df.drop_duplicates(subset=["tx_hash", "log_index"])
    _enrich_usd(df)
    df["block_number"] = df["block_number"].astype("int64")
    df["chain_id"] = df["chain_id"].astype("int32")
    df["log_index"] = df["log_index"].astype("int32")
    df["block_timestamp"] = pd.to_datetime(df["block_timestamp"], utc=True)
    df = df.sort_values(["block_number", "log_index"], kind="mergesort").reset_index(
        drop=True
    )[_SCHEMA_COLUMNS]
    _OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(_OUT_PARQUET, index=False)
    print(f"\nWrote {len(df)} launch-month rows to {_OUT_PARQUET}")
    print(df["event_type"].value_counts().to_string())


if __name__ == "__main__":
    main()
