"""D-A cascade labeler, implementing ADR-001 exactly.

Pipeline: load liquidation events for a protocol set → for each window size
`w`, compute per-anchor rolling-window stats (severity, breadth, generations)
in one pass → for each (k, theta) grid point, threshold + merge overlapping
candidate windows into episodes.

See docs/decisions/ADR-001-cascade-definition.md for the definition this
code must not silently drift from.
"""

from __future__ import annotations

import glob
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

# ADR-001 fixed constant — not part of the grid.
GENERATION_LAG_BLOCKS = 20

W_GRID = (50, 100, 300)
K_GRID = (5, 10, 20)
THETA_GRID = {"p99": 99.0, "p99.5": 99.5, "p99.9": 99.9}
PRIMARY_GRID_POINT = (100, 10, "p99.5")

DEFAULT_PROTOCOLS = ("aave_v2",)

LIQUIDATION_EVENT_TYPES = frozenset(
 {
 "LiquidationCall", # Aave v2/v3
 "LiquidateBorrow", # Compound v2
 "Absorb", # Compound v3 (Comet's actual liquidation event name)
 "Bite", # Maker (Cat)
 "Bark", # Maker (Dog)
 }
)


@dataclass(frozen=True)
class GridPoint:
 w: int
 k: int
 theta_label: str

 @property
 def is_primary(self) -> bool:
 return (self.w, self.k, self.theta_label) == PRIMARY_GRID_POINT


def load_liquidations(
 data_dir: str | Path = "data/raw",
 protocols: Sequence[str] = DEFAULT_PROTOCOLS,
) -> pd.DataFrame:
 """Load + concatenate liquidation events for the given protocols.

 Protocol directories also contain non-liquidation core events (Deposit,
 Borrow, ...); filter down to liquidation event types after loading.
 """
 data_dir = Path(data_dir)
 frames = []
 for protocol in protocols:
 paths = sorted(glob.glob(str(data_dir / protocol / "chain=*" / "*.parquet")))
 if not paths:
 raise FileNotFoundError(
 f"No parquet files found for protocol {protocol!r} under {data_dir}"
 )
 for path in paths:
 frames.append(pd.read_parquet(path))

 df = pd.concat(frames, ignore_index=True)
 df = df[df["event_type"].isin(LIQUIDATION_EVENT_TYPES)].copy
 # (tx_hash, log_index) alone is too coarse: Compound v3 batch absorbs
 # legitimately emit multiple rows (one per absorbed user) under one
 # log_index -- include user so those aren't collapsed.
 df = df.drop_duplicates(subset=["tx_hash", "log_index", "user"])
 df = df.sort_values("block_number", kind="mergesort").reset_index(drop=True)
 return _with_position_id(df)


def _with_position_id(df: pd.DataFrame) -> pd.DataFrame:
 if "position_id" in df.columns:
 return df
 df = df.copy
 df["position_id"] = (
 df["user"].fillna("")
 + "|"
 + df["collateral_asset"].fillna("")
 + "|"
 + df["debt_asset"].fillna("")
 )
 return df


def _max_generations(blocks: np.ndarray, assets: np.ndarray, lag: int) -> int:
 """ADR-001 generation-linking: per shared collateral asset, split
 chronologically-sorted liquidations into waves separated by > lag
 blocks; the window's generation count is the max wave count over assets.
 """
 if len(blocks) == 0:
 return 0
 best = 1
 for asset in np.unique(assets):
 asset_blocks = np.sort(blocks[assets == asset])
 gens = 1
 for j in range(1, len(asset_blocks)):
 if asset_blocks[j] - asset_blocks[j - 1] > lag:
 gens += 1
 best = max(best, gens)
 return best


def _window_stats_for_w(df: pd.DataFrame, w: int, lag: int) -> pd.DataFrame:
 """For every liquidation event i (anchor), aggregate stats over the
 half-open w-block window [block_i, block_i + w).
 """
 blocks = df["block_number"].to_numpy
 usd = df["amount_usd"].fillna(0.0).to_numpy
 users = df["user"].to_numpy
 assets = df["collateral_asset"].fillna("unknown").to_numpy
 positions = df["position_id"].to_numpy
 n = len(df)

 # blocks is sorted ascending, so blocks + w is too -> one vectorized call.
 end_idx = np.searchsorted(blocks, blocks + w, side="left")

 total_usd = np.empty(n)
 num_positions = np.empty(n, dtype=np.int32)
 num_accounts = np.empty(n, dtype=np.int32)
 max_generations = np.empty(n, dtype=np.int32)
 end_block = np.empty(n, dtype=np.int64)

 for i in range(n):
 j = end_idx[i]
 total_usd[i] = usd[i:j].sum
 num_positions[i] = len(set(positions[i:j]))
 num_accounts[i] = len(set(users[i:j]))
 max_generations[i] = _max_generations(blocks[i:j], assets[i:j], lag)
 end_block[i] = blocks[j - 1]

 return pd.DataFrame(
 {
 "start_block": blocks,
 "end_block": end_block,
 "total_usd": total_usd,
 "num_positions": num_positions,
 "num_accounts": num_accounts,
 "max_generations": max_generations,
 }
 )


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
 if not intervals:
 return []
 intervals = sorted(intervals)
 merged = [intervals[0]]
 for s, e in intervals[1:]:
 last_s, last_e = merged[-1]
 if s <= last_e:
 merged[-1] = (last_s, max(last_e, e))
 else:
 merged.append((s, e))
 return merged


def _aggregate_range(
 df: pd.DataFrame, blocks: np.ndarray, start_block: int, end_block: int, lag: int
) -> dict:
 lo = np.searchsorted(blocks, start_block, side="left")
 hi = np.searchsorted(blocks, end_block, side="right")
 sub = df.iloc[lo:hi]
 return {
 "start_time": sub["block_timestamp"].min,
 "end_time": sub["block_timestamp"].max,
 "total_liquidated_usd": sub["amount_usd"].fillna(0.0).sum,
 "num_positions": sub["position_id"].nunique,
 "num_accounts": sub["user"].nunique,
 "max_generations": _max_generations(
 sub["block_number"].to_numpy,
 sub["collateral_asset"].fillna("unknown").to_numpy,
 lag,
 ),
 }


def label_dataframe(
 df: pd.DataFrame,
 protocol_tag: str,
 w_grid: Sequence[int] = W_GRID,
 k_grid: Sequence[int] = K_GRID,
 theta_grid: dict[str, float] = THETA_GRID,
 lag: int = GENERATION_LAG_BLOCKS,
) -> pd.DataFrame:
 """Run the D-A labeler across the full (w, k, theta) grid.

 `df` must already be filtered to liquidation events and sorted by
 block_number (see `load_liquidations`). Returns one row per cascade
 episode per grid point, matching `schema.CASCADE_LABEL_SCHEMA`.
 """
 df = _with_position_id(df).reset_index(drop=True)
 blocks = df["block_number"].to_numpy
 episodes: list[dict] = []

 for w in w_grid:
 stats = _window_stats_for_w(df, w, lag)
 for theta_label, theta_pct in theta_grid.items:
 theta_usd = float(np.percentile(stats["total_usd"], theta_pct))
 for k in k_grid:
 mask = (
 (stats["total_usd"] >= theta_usd)
 & (stats["num_positions"] >= k)
 & (stats["num_accounts"] >= 2)
 & (stats["max_generations"] >= 2)
 )
 candidate_intervals = list(
 zip(stats.loc[mask, "start_block"], stats.loc[mask, "end_block"])
 )
 for start_block, end_block in _merge_intervals(candidate_intervals):
 agg = _aggregate_range(df, blocks, start_block, end_block, lag)
 grid_point = GridPoint(w=w, k=k, theta_label=theta_label)
 episode_id = f"{protocol_tag}_w{w}_k{k}_{theta_label}_{start_block}_{end_block}"
 episodes.append(
 {
 "episode_id": episode_id,
 "protocol_tag": protocol_tag,
 "definition": "D-A",
 "w": w,
 "k": k,
 "theta_label": theta_label,
 "theta_usd": theta_usd,
 "generation_lag_blocks": lag,
 "is_primary": grid_point.is_primary,
 "start_block": int(start_block),
 "end_block": int(end_block),
 "severity_usd": agg["total_liquidated_usd"],
 **agg,
 }
 )

 columns = [
 "episode_id",
 "protocol_tag",
 "definition",
 "w",
 "k",
 "theta_label",
 "theta_usd",
 "generation_lag_blocks",
 "is_primary",
 "start_block",
 "end_block",
 "start_time",
 "end_time",
 "total_liquidated_usd",
 "num_positions",
 "num_accounts",
 "max_generations",
 "severity_usd",
 ]
 if not episodes:
 return pd.DataFrame(columns=columns)
 return pd.DataFrame(episodes)[columns]
