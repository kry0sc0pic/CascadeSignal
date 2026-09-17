"""Stress-window detection for denser contagion-graph snapshots.

Graph cadence: snapshots every 25 blocks normally, every 5 blocks "in stress
windows". A window is "stress" if a bar's liquidation-USD activity is a
causal outlier against its own trailing history -- the standard
z-score-against-trailing-window idea, thresholded into a binary flag here.
Operates on any bar-level DataFrame with a `liq_usd_col` and `start_block`,
sharing the project's bar cadence and as-of discipline.

Causality: bar i's mean/std come only from bars strictly before it
(`shift(1)` before the rolling window) -- a bar's own spike never
contributes to its own baseline, and no bar's flag depends on a later bar.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from cascadesignal.graph.build import (
 DEFAULT_SNAPSHOT_CADENCE_BLOCKS,
 STRESS_SNAPSHOT_CADENCE_BLOCKS,
)

DEFAULT_WINDOW_BARS = 576 # ~2 days of trailing history at 5-block bars
DEFAULT_MIN_PERIODS = 48
DEFAULT_Z_THRESHOLD = 3.0


def detect_stress_windows(
 bars: pd.DataFrame,
 *,
 liq_usd_col: str = "liq_usd",
 window_bars: int = DEFAULT_WINDOW_BARS,
 min_periods: int = DEFAULT_MIN_PERIODS,
 z_threshold: float = DEFAULT_Z_THRESHOLD,
) -> list[tuple[int, int]]:
 """Contiguous `[start_block, end_block)` windows where `liq_usd_col` is a
 causal outlier (z-score >= `z_threshold` against its own trailing
 `window_bars`-bar history). Bars before `min_periods` of history have
 accumulated can't be flagged (undefined baseline), the standard
 cold-start handling for a rolling-window baseline.
 """
 if bars.empty:
 return []

 ordered = bars.sort_values("start_block", kind="mergesort").reset_index(drop=True)
 values = ordered[liq_usd_col].astype(float)
 trailing_mean = values.shift(1).rolling(window_bars, min_periods=min_periods).mean
 trailing_std = (
 values.shift(1).rolling(window_bars, min_periods=min_periods).std(ddof=0)
 )
 z = (values - trailing_mean) / trailing_std.replace(0.0, np.nan)
 is_stress = (z >= z_threshold).fillna(False).to_numpy

 start_blocks = ordered["start_block"].to_numpy
 end_blocks = ordered["end_block"].to_numpy

 windows: list[tuple[int, int]] = []
 window_start = None
 for i, flagged in enumerate(is_stress):
 if flagged and window_start is None:
 window_start = int(start_blocks[i])
 elif not flagged and window_start is not None:
 windows.append((window_start, int(start_blocks[i])))
 window_start = None
 if window_start is not None:
 windows.append((window_start, int(end_blocks[-1])))
 return windows


def snapshot_blocks_with_stress(
 start_block: int,
 end_block: int,
 stress_windows: list[tuple[int, int]],
 *,
 normal_cadence: int = DEFAULT_SNAPSHOT_CADENCE_BLOCKS,
 stress_cadence: int = STRESS_SNAPSHOT_CADENCE_BLOCKS,
) -> np.ndarray:
 """Snapshot block anchors over `[start_block, end_block)`, spaced at
 `stress_cadence` whenever a normal-cadence step from the current anchor
 would pass through (or already sits inside) any `stress_windows`
 interval, `normal_cadence` otherwise.

 This checks the upcoming `[block, block + normal_cadence)` interval, not
 just whether `block` itself is in a window: a window only a handful of
 blocks wide can otherwise sit entirely between two coarse anchors and
 never trigger dense sampling at all (found in production at the real
 7200/1440-block cadence -- narrow stress windows were being silently
 stepped over almost everywhere; see). This is
 look-ahead over `stress_windows`, which is fine here: those windows are
 precomputed from the *entire* historical bars file before this function
 ever runs (an offline materialization decision, not a live per-block
 inference the model makes), so peeking at them doesn't leak information
 into any snapshot's own feature values -- it only decides where to
 place snapshot anchors.
 """
 if normal_cadence <= 0 or stress_cadence <= 0:
 raise ValueError("cadences must be positive")
 if end_block <= start_block:
 raise ValueError("end_block must be after start_block")

 windows = sorted(stress_windows, key=lambda w: w[0])

 def _step_overlaps_stress(block: int) -> bool:
 hi_bound = block + normal_cadence
 for lo, hi in windows:
 if lo < hi_bound and hi > block:
 return True
 if lo >= hi_bound:
 break
 return False

 blocks: list[int] = []
 block = start_block
 while block < end_block:
 blocks.append(block)
 block += stress_cadence if _step_overlaps_stress(block) else normal_cadence
 return np.array(blocks, dtype=np.int64)


__all__ = [
 "detect_stress_windows",
 "snapshot_blocks_with_stress",
 "DEFAULT_WINDOW_BARS",
 "DEFAULT_MIN_PERIODS",
 "DEFAULT_Z_THRESHOLD",
]
