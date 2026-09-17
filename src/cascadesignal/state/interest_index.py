"""Aave v2 interest-index lookups.

Backed by `data/raw/aave_v2_reserve_index/` (`ReserveDataUpdated`). Two
sources feed this directory: the original Dune-based
`cascadesignal.ingest.reserve_index.ReserveIndexIngester` (5 golden
cascade-episode windows, priced out of a full pull at ~7,000-27,500 Dune
credits against a 5,000-credit budget), and
`scripts/onchain/backfill_reserve_index.py`, an Etherscan-
based puller that is NOT credit-metered and covers the full
[11,500,000, 24,500,000] block range in one pass -- run once, it made
coverage continuous rather than 5 disjoint windows. `coverage` still
reports per-reserve contiguous runs (not a single hardcoded assumption) so
callers correctly see partial coverage for anything pulled before Track D
existed or outside its block range. `engine.PositionStateEngine`'s balance replay
(scaling each ledger row's raw delta by the index at its own event block
Aave's own "scaledBalance" pattern -- then reinflating by the index at query
time). Coverage can still be partial for any *given* row (blocks before
11.5M, or a reserve added mid-history) -- rather than an all-or-nothing gate
per user (which would silently leave some users fully non-accruing and
others fully accruing, an inconsistency worse than uniform non-accrual),
each row falls back independently to a 1.0 multiplier when its own
(reserve, block) isn't covered, degrading gracefully to the pre-Track-E
value for just that delta. `index_at_many` is the vectorized form
`PositionStateEngine` uses for this (a per-row Python loop over a
multi-million-row ledger would be too slow).
`scripts/analysis/estimate_interest_accrual_gap.py` separately *quantifies*
the effect's plausible size against real per-account holding periods,
rather than assuming it.

Index values are RAY-scaled (1e27) fixed point, matching Aave v2's on-chain
representation; `index_at`/`index_at_many` return the unscaled
(~1.0-and-growing) float. (2026-07-22): `index_at`/`index_at_many` return the index as of
its last on-chain *write* (a `ReserveDataUpdated` event, which only fires
when someone interacts with that reserve) -- but Aave's real `balanceOf`
compounds the stored index forward to the *current* second using the stored
rate, every time it's read, whether or not anyone has interacted with the
reserve since. An optional `query_timestamp`/`query_timestamps` argument
replicates that: `calculate_linear_interest` (liquidity index -- deposits
accrue simple/linear interest) and `calculate_compounded_interest` (variable
borrow index -- debt compounds, approximated on-chain via a 2nd/3rd-order
binomial expansion, `MathUtils.calculateCompoundedInterest`) both match
Aave v2's real Solidity formulas exactly, just in plain float arithmetic
instead of RAY-scaled integers (equivalent up to float precision, since RAY
cancels consistently through every term). Omitting the timestamp preserves
the exact pre-H5 behavior (every other caller of this oracle).
"""

from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

_RAY = 1e27
_SECONDS_PER_YEAR = 365 * 24 * 60 * 60 # Aave v2 MathUtils.SECONDS_PER_YEAR


def calculate_linear_interest(rate: float, elapsed_seconds: float) -> float:
 """Aave v2 `MathUtils.calculateLinearInterest` -- the liquidity
 (aToken/collateral) index's forward-compounding formula. Returns a
 multiplier (1.0 at elapsed=0, growing linearly with time)."""
 if elapsed_seconds <= 0:
 return 1.0
 return (rate * elapsed_seconds) / _SECONDS_PER_YEAR + 1.0


def calculate_compounded_interest(rate: float, elapsed_seconds: float) -> float:
 """Aave v2 `MathUtils.calculateCompoundedInterest` -- the variable-debt
 index's forward-compounding formula: a 2nd/3rd-order binomial
 approximation of continuous compounding (matching the real on-chain
 `balanceOf`, not true e^(rt) compounding). Returns a multiplier."""
 exp = elapsed_seconds
 if exp <= 0:
 return 1.0
 exp_minus_one = exp - 1
 exp_minus_two = exp - 2 if exp > 2 else 0
 rate_per_second = rate / _SECONDS_PER_YEAR
 base_power_two = rate_per_second * rate_per_second
 base_power_three = base_power_two * rate_per_second
 second_term = exp * exp_minus_one * base_power_two / 2
 third_term = exp * exp_minus_one * exp_minus_two * base_power_three / 6
 return 1.0 + rate_per_second * exp + second_term + third_term


def _compounded_interest_array(rate: np.ndarray, elapsed: np.ndarray) -> np.ndarray:
 """Vectorized `calculate_compounded_interest`, for `index_at_many`'s
 batched path (a Python loop over tens of thousands of rows would defeat
 the point of the `merge_asof`-based vectorization elsewhere in this
 module). Identical formula, `np.where`-guarded per-element instead of an
 early return."""
 exp = np.maximum(elapsed, 0.0)
 exp_minus_one = exp - 1
 exp_minus_two = np.where(exp > 2, exp - 2, 0.0)
 rate_per_second = rate / _SECONDS_PER_YEAR
 base_power_two = rate_per_second * rate_per_second
 base_power_three = base_power_two * rate_per_second
 second_term = exp * exp_minus_one * base_power_two / 2
 third_term = exp * exp_minus_one * exp_minus_two * base_power_three / 6
 multiplier = 1.0 + rate_per_second * exp + second_term + third_term
 return np.where(elapsed > 0, multiplier, 1.0)


class InterestIndexOracle:
 """Nearest-prior-block lookup for Aave v2's liquidity/variableBorrow index."""

 def __init__(self, data_dir: str | Path = "data/raw"):
 data_dir = Path(data_dir)
 paths = sorted(
 glob.glob(str(data_dir / "aave_v2_reserve_index" / "chain=*" / "*.parquet"))
 )
 if not paths:
 raise FileNotFoundError(
 f"No reserve-index parquet found under {data_dir}/aave_v2_reserve_index/ "
 "-- run cascadesignal.ingest.reserve_index.ReserveIndexIngester first."
 )
 df = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
 df = df.sort_values("block_number")
 self._by_reserve: dict[str, pd.DataFrame] = {
 str(reserve): group[
 [
 "block_number",
 "block_timestamp",
 "liquidity_index_raw",
 "variable_borrow_index_raw",
 "liquidity_rate_raw",
 "variable_borrow_rate_raw",
 ]
 ].reset_index(drop=True)
 for reserve, group in df.groupby("reserve")
 }

 def coverage(self) -> dict[str, list[tuple[int, int]]]:
 """Per-reserve list of contiguous (min_block, max_block) windows.

 Returns one (start, end) pair per contiguous run of blocks (gaps
 > `_MAX_GAP_BLOCKS` start a new window). Since Track D's full-range
 Etherscan pull, most reserves report a single continuous window
 covering [11.5M, 24.5M]; this stays generic rather than assuming
 that, since older Dune-only data or blocks outside that range can
 still leave real gaps for a given reserve.
 """
 out: dict[str, list[tuple[int, int]]] = {}
 for reserve, g in self._by_reserve.items:
 blocks = g["block_number"].to_numpy
 windows = []
 start = blocks[0]
 prev = blocks[0]
 for b in blocks[1:]:
 if b - prev > self._MAX_GAP_BLOCKS:
 windows.append((int(start), int(prev)))
 start = b
 prev = b
 windows.append((int(start), int(prev)))
 out[reserve] = windows
 return out

 # Defensive guard for any residual gap (pre-Track D data was 5 disjoint
 # golden-episode windows -- e.g. China's window ended ~block 12.77M and
 # Terra's started ~14.7M, a ~2M-block/~280-day gap). Without a cap,
 # "nearest prior" would silently bridge a gap and return stale data on
 # its far side. 100k blocks (~14 days) comfortably covers normal
 # within-coverage sparsity (not every reserve updates every block) while
 # still rejecting real cross-gap bleed.
 _MAX_GAP_BLOCKS = 100_000

 def _lookup_row(self, reserve: str, block_number: int) -> pd.Series | None:
 group = self._by_reserve.get(reserve.lower)
 if group is None or group.empty:
 return None
 idx = int(group["block_number"].searchsorted(block_number, side="right")) - 1
 if idx < 0:
 return None
 row = group.iloc[idx]
 if block_number - int(row["block_number"]) > self._MAX_GAP_BLOCKS:
 return None
 return row

 def _lookup(
 self, reserve: str, block_number: int, kind: str, rate_or_index: str
 ) -> float | None:
 row = self._lookup_row(reserve, block_number)
 if row is None:
 return None
 col = f"{kind}_{rate_or_index}_raw"
 return float(row[col]) / _RAY

 def index_at(
 self,
 reserve: str,
 block_number: int,
 kind: str = "liquidity",
 query_timestamp: pd.Timestamp | None = None,
 ) -> float | None:
 """Nearest available index at or before `block_number`.

 `kind` is "liquidity" (aToken/collateral growth) or "variable_borrow"
 (variable-debt growth). Returns None if this reserve has no index
 data within `_MAX_GAP_BLOCKS` at or before `block_number` -- either
 outside pulled-window coverage entirely, or on the far side of a
 gap between two disjoint windows.

 `query_timestamp`: if given, forward-compounds the
 matched stored index to this exact moment using the rate stored
 alongside it (see module docstring) -- omit for the pre-H5 "index as
 of its last on-chain write" behavior.
 """
 row = self._lookup_row(reserve, block_number)
 if row is None:
 return None
 stored = float(row[f"{kind}_index_raw"]) / _RAY
 if query_timestamp is None:
 return stored
 elapsed = (query_timestamp - row["block_timestamp"]).total_seconds
 rate = float(row[f"{kind}_rate_raw"]) / _RAY
 multiplier = (
 calculate_linear_interest(rate, elapsed)
 if kind == "liquidity"
 else calculate_compounded_interest(rate, elapsed)
 )
 return stored * multiplier

 def rate_at(
 self, reserve: str, block_number: int, kind: str = "liquidity"
 ) -> float | None:
 """Nearest available instantaneous APR (as a fraction, e.g. 0.03 = 3%).

 Same `_MAX_GAP_BLOCKS` cross-window guard as `index_at`.
 """
 return self._lookup(reserve, block_number, kind, "rate")

 def index_at_many(
 self,
 reserves: pd.Series,
 blocks: pd.Series,
 kind: str = "liquidity",
 query_timestamps: pd.Series | None = None,
 ) -> pd.Series:
 """Vectorized `index_at` for many (reserve, block_number) pairs at
 once -- one `merge_asof` per distinct reserve rather than a per-row
 Python loop, for `PositionStateEngine`'s multi-million-row ledger
 (Track E). Returns a float Series aligned to `reserves`'
 original index, with NaN wherever `index_at` would return None.

 `query_timestamps`: same forward-compounding as
 `index_at`'s `query_timestamp`, vectorized via `_compounded_interest_array`
 (liquidity's linear formula vectorizes trivially inline).
 """
 col = f"{kind}_index_raw"
 rate_col = f"{kind}_rate_raw"
 frame = pd.DataFrame(
 {
 "reserve": reserves.astype(str).str.lower.to_numpy,
 "block_number": pd.to_numeric(blocks).to_numpy,
 },
 index=reserves.index,
 )
 if query_timestamps is not None:
 frame["query_timestamp"] = pd.to_datetime(
 pd.Series(query_timestamps, index=reserves.index)
 ).to_numpy

 result = pd.Series(float("nan"), index=frame.index, dtype="float64")
 for reserve, group in frame.groupby("reserve", sort=False):
 table = self._by_reserve.get(str(reserve))
 if table is None or table.empty:
 continue
 ordered = group.sort_values("block_number", kind="mergesort")
 merge_cols = ["block_number", col]
 if query_timestamps is not None:
 merge_cols += ["block_timestamp", rate_col]
 merged = pd.merge_asof(
 ordered,
 table[merge_cols].sort_values("block_number", kind="mergesort"),
 on="block_number",
 direction="backward",
 tolerance=self._MAX_GAP_BLOCKS,
 )
 merged.index = ordered.index
 stored = merged[col].astype(float) / _RAY
 if query_timestamps is None:
 result.loc[merged.index] = stored
 continue
 elapsed = (
 merged["query_timestamp"] - merged["block_timestamp"]
 ).dt.total_seconds
 rate = merged[rate_col].astype(float) / _RAY
 if kind == "liquidity":
 multiplier = np.where(
 elapsed > 0, (rate * elapsed) / _SECONDS_PER_YEAR + 1.0, 1.0
 )
 else:
 multiplier = _compounded_interest_array(
 rate.to_numpy, elapsed.to_numpy
 )
 result.loc[merged.index] = stored.to_numpy * multiplier
 return result
