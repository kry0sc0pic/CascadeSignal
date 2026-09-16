"""Fixed-origin bar clock for live scoring.

`models/labels.py:build_liquidation_bars` bins a liquidation stream in one
`np.histogram` call over `[blocks.min(), blocks.max()]` of *whatever data
happens to be loaded* -- fine for a static backtest, but a live daemon can't
call it per-tick on a growing array: the bar edges would silently shift
every time new data arrives, since the last bar's boundary depends on
`blocks[-1]`. This module anchors bar edges to a fixed origin block instead
(the offline fit's last historical `end_block`, so the live series is a
direct continuation with no gap or overlap at the handoff point), and only
closes a bar once it is entirely at or below the chain's finalized head --
so a bar, once closed, never needs to be revised by a reorg.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def closed_bar_edges(origin_block: int, bar_blocks: int, through_block: int) -> np.ndarray:
    """Bar edges origin, origin+bar_blocks, ... up to the last edge <=
    through_block (i.e. every bar fully closed as of `through_block`)."""
    if through_block < origin_block:
        return np.array([origin_block], dtype=np.int64)
    n_bars = (through_block - origin_block) // bar_blocks
    return origin_block + np.arange(0, n_bars + 1, dtype=np.int64) * bar_blocks


def bin_liquidations(
    blocks: np.ndarray, origin_block: int, bar_blocks: int, through_block: int
) -> pd.DataFrame:
    """Bin liquidation block numbers into fixed-origin bars closed as of
    `through_block`. Same `n_liquidations`/`start_block`/`end_block` shape as
    `build_liquidation_bars` (no `end_time` -- the Hawkes model never reads
    it; only the offline walk-forward splitter does)."""
    edges = closed_bar_edges(origin_block, bar_blocks, through_block)
    if len(edges) < 2:
        return pd.DataFrame(
            {"start_block": [], "end_block": [], "n_liquidations": []}
        ).astype({"start_block": "int64", "end_block": "int64", "n_liquidations": "int64"})
    n_liquidations, _ = np.histogram(blocks, bins=edges)
    return pd.DataFrame(
        {
            "start_block": edges[:-1],
            "end_block": edges[1:],
            "n_liquidations": n_liquidations,
        }
    )


__all__ = ["closed_bar_edges", "bin_liquidations"]
