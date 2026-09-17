"""Derive synthetic `AnswerUpdated` history for xSUSHI's real Aave oracle
adapter (Lever 11c), from the events
`backfill_xsushi_underlying_events.py` pulled.

`XSushiPriceAdapter.latestAnswer` computes, fresh on every call:

 exchangeRate = SUSHI.balanceOf(xSUSHI) * 1 ether / xSUSHI.totalSupply
 answer = SUSHI_ORACLE.latestAnswer * exchangeRate / 1 ether

Three live inputs, not one: `SUSHI.balanceOf(xSUSHI)` and
`xSUSHI.totalSupply` (both reconstructed as running cumulative sums over
their respective `Transfer` event histories -- see the pull script's
docstring) and `SUSHI_ORACLE.latestAnswer` (an ordinary 3-phase Chainlink
feed). Same exact-replication approach as
`derive_custom_adapter_eth_feeds.py`'s 2-input cross-rate, generalized to 3:
materialize one row at *every* block where *any* of the three inputs
changes, each paired with the other two's nearest-prior-or-equal value
between any two consecutive breakpoints none of the three has moved, so the
computed answer is constant there, and "nearest-prior row in the synthetic
series" reproduces exactly what a fresh on-chain call would return at any
query point in between.

All arithmetic uses plain Python ints, matching Solidity's own integer
(floor) division exactly -- no float rounding. A block where
`totalSupply == 0` (before the SushiBar's very first deposit) would revert
on-chain -- skipped here rather than emitting a nonsensical answer, same as
every other adapter's own `<= 0` guard.

Usage:
 python scripts/onchain/derive_xsushi_eth_feed.py
Writes `data/raw/chainlink/chain=1/derived_xsushi_eth_feed.parquet`.
"""

from __future__ import annotations

import sys
from itertools import accumulate
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve.parents[2] / "src"))
from cascadesignal.ingest.schema import normalize, to_arrow # noqa: E402

_ORACLE_PARQUET = Path("data/raw/chainlink/chain=1/sushi_oracle_answer_updated.parquet")
_EVENTS_PARQUET = Path("data/raw/sushibar/chain=1/sushibar_events.parquet")
_OUT_PARQUET = Path("data/raw/chainlink/chain=1/derived_xsushi_eth_feed.parquet")

_ORDER_KEY_LOG_INDEX_MULTIPLIER = 1_000_000 # matches prices._order_key

# Real wrapper contract address (Aave's own `AssetSourceUpdated` history)
# used as the synthetic `user` (aggregator) key.
_XSUSHI_WRAPPER = "0x9b26214bec078e68a394aaebfbfff406ce14893f"


def _order_key(block_number: np.ndarray, log_index: np.ndarray) -> np.ndarray:
 return block_number.astype(
 np.int64
 ) * _ORDER_KEY_LOG_INDEX_MULTIPLIER + log_index.astype(np.int64)


def _cumulative_series(events: pd.DataFrame, kind: str) -> pd.DataFrame:
 """Running cumulative sum of `signed_amount` for one event kind, ordered
 by (block_number, log_index) -- the value immediately after each event.

 `signed_amount` is stored as a string (18-decimal raw token amounts
 routinely exceed int64) -- parsed to Python's arbitrary-precision `int`
 and accumulated via `itertools.accumulate`, not `Series.cumsum` (which
 would silently upcast to a fixed-width numpy type and overflow the same
 way)."""
 sub = events[events["kind"] == kind].sort_values(
 ["block_number", "log_index"], kind="mergesort"
 )
 sub = sub.drop_duplicates(subset=["block_number", "log_index"], keep="last")
 out = sub[["block_number", "log_index", "block_timestamp", "tx_hash"]].copy
 amounts = (int(v) for v in sub["signed_amount"])
 out["value"] = list(accumulate(amounts))
 return out.reset_index(drop=True)


def _merge_oracle_series(raw: pd.DataFrame, addresses: list[str]) -> pd.DataFrame:
 sub = raw[raw["user"].isin([a.lower for a in addresses])]
 out = sub[["block_number", "log_index", "block_timestamp", "tx_hash", "value"]]
 return (
 out.sort_values(["block_number", "log_index"])
 .drop_duplicates(subset=["block_number", "log_index"], keep="last")
 .reset_index(drop=True)
 )


def _nearest_prior_value(
 series: pd.DataFrame, order_keys: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
 series_keys = _order_key(
 series["block_number"].to_numpy, series["log_index"].to_numpy
 )
 idx = np.searchsorted(series_keys, order_keys, side="right") - 1
 values = series["value"].to_numpy
 out = np.zeros(len(order_keys), dtype=object)
 valid = idx >= 0
 out[valid] = values[idx[valid]]
 return out, valid


def _combine_three_feeds(
 balance: pd.DataFrame, supply: pd.DataFrame, price: pd.DataFrame
) -> pd.DataFrame:
 """Union of all three feeds' breakpoints, each paired with the OTHER
 two's nearest-prior-or-equal value -- see module docstring."""
 named = {"balance": balance, "supply": supply, "price": price}
 rows = []
 for own_name, own in named.items:
 own_keys = _order_key(
 own["block_number"].to_numpy, own["log_index"].to_numpy
 )
 other_values = {}
 all_valid = np.ones(len(own), dtype=bool)
 for other_name, other in named.items:
 if other_name == own_name:
 continue
 values, valid = _nearest_prior_value(other, own_keys)
 other_values[other_name] = values
 all_valid &= valid
 for i in range(len(own)):
 if not all_valid[i]:
 continue
 row = {
 "block_number": int(own["block_number"].iloc[i]),
 "log_index": int(own["log_index"].iloc[i]),
 "block_timestamp": own["block_timestamp"].iloc[i],
 "tx_hash": own["tx_hash"].iloc[i],
 own_name: int(own["value"].iloc[i]),
 }
 for other_name, values in other_values.items:
 row[other_name] = int(values[i])
 rows.append(row)
 combined = pd.DataFrame(rows)
 combined = combined.sort_values(["block_number", "log_index"]).drop_duplicates(
 subset=["block_number", "log_index"], keep="last"
 )
 return combined.reset_index(drop=True)


def main -> None:
 oracle_raw = pd.read_parquet(_ORACLE_PARQUET)
 oracle_raw = oracle_raw[oracle_raw["event_type"] == "AnswerUpdated"].copy
 oracle_raw["user"] = oracle_raw["user"].str.lower
 oracle_raw["value"] = pd.to_numeric(oracle_raw["amount_raw"], errors="coerce")
 oracle_raw = oracle_raw.dropna(subset=["value"])
 oracle_raw["value"] = oracle_raw["value"].astype("int64")
 price = _merge_oracle_series(oracle_raw, list(oracle_raw["user"].unique))

 events = pd.read_parquet(_EVENTS_PARQUET)
 balance = _cumulative_series(events, "sushi_balance")
 supply = _cumulative_series(events, "xsushi_supply")

 print(f"SUSHI/ETH price: {len(price)} rows")
 print(
 f"SUSHI balance-of-xSUSHI: {len(balance)} rows, "
 f"final value={balance['value'].iloc[-1]}"
 )
 print(
 f"xSUSHI totalSupply: {len(supply)} rows, "
 f"final value={supply['value'].iloc[-1]}"
 )

 combined = _combine_three_feeds(balance, supply, price)
 print(f"combined breakpoints (all 3 inputs valid): {len(combined)}")

 rows: list[dict] = []
 n_skipped_nonpositive = 0
 for _, row in combined.iterrows:
 supply_val = row["supply"]
 balance_val = row["balance"]
 price_val = row["price"]
 if supply_val <= 0 or price_val <= 0:
 n_skipped_nonpositive += 1
 continue
 exchange_rate = (balance_val * 10**18) // supply_val
 answer = (price_val * exchange_rate) // 10**18
 if answer <= 0:
 n_skipped_nonpositive += 1
 continue
 rows.append(
 {
 "chain_id": 1,
 "block_number": int(row["block_number"]),
 "block_timestamp": row["block_timestamp"],
 "tx_hash": row["tx_hash"],
 "log_index": int(row["log_index"]),
 "protocol": "chainlink",
 "event_type": "AnswerUpdated",
 "user": _XSUSHI_WRAPPER,
 "collateral_asset": None,
 "debt_asset": None,
 "amount_raw": str(answer),
 "amount_usd": answer / 1e18,
 "liquidator": None,
 "collateral_seized_raw": None,
 "collateral_seized_usd": None,
 }
 )
 print(f"skipped (non-positive supply/price/answer): {n_skipped_nonpositive}")

 df = normalize(pd.DataFrame(rows), protocol="chainlink")
 df = df.drop_duplicates(subset=["user", "block_number", "log_index"])
 _OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
 to_arrow(df).to_pandas.to_parquet(_OUT_PARQUET, index=False)
 print(f"\nWrote {len(df)} rows to {_OUT_PARQUET}")


if __name__ == "__main__":
 main
