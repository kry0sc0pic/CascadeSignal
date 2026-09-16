"""Per-bar forecast labels for the walk-forward harness (CAS-24).

Turns the CAS-16 cascade *episodes* (spans) into a per-bar binary *forecast*
target aligned to the CAS-20 feature bars: a bar is positive iff a cascade
episode BEGINS within the next `horizon_blocks` after the bar's as-of cutoff
(`end_block`). This is the RQ1 "predict a cascade h blocks ahead" framing.

The label uses `end_block` (the bar's as-of cutoff) as the anchor, so the
forecast window (end_block, end_block + horizon] is strictly in the future
relative to every feature in that bar -- no leakage.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def build_liquidation_bars(liq_df: pd.DataFrame, bar_blocks: int = 5) -> pd.DataFrame:
    """Bin a protocol's liquidation stream into fixed-`bar_blocks`-width bars.

    Gives the Hawkes alarm the only mark it needs (`n_liquidations`) without
    the cut feature-bar pipeline: `start_block`/`end_block` from a plain
    `np.histogram` over block-edges spanning the stream's own block range,
    and `end_time` via `np.interp` against the stream's own (block,
    timestamp) pairs, so empty (quiet) bars still get a timestamp for the
    walk-forward splitter's monthly folds instead of being dropped.

    Args:
        liq_df: liquidation events with `block_number` and `block_timestamp`
            (see `labels/cascade_labeler.py:load_liquidations`).
        bar_blocks: bar width in blocks.

    Returns:
        One row per bar, columns `start_block`, `end_block`, `end_time`,
        `n_liquidations`, and (when the stream carries `amount_usd`)
        `liquidated_usd` = total USD liquidated in the bar, sorted by
        `end_block`. `liquidated_usd` is the mark for the USD-marked Hawkes
        (ADR-007); the count model ignores it.
    """
    order = np.argsort(
        liq_df["block_number"].to_numpy(dtype=np.int64), kind="mergesort"
    )
    blocks = liq_df["block_number"].to_numpy(dtype=np.int64)[order]
    timestamps_ns = (
        pd.to_datetime(liq_df["block_timestamp"].to_numpy()[order], utc=True)
        .as_unit("ns")
        .values.astype(np.int64)
    )

    edges = np.arange(blocks[0], blocks[-1] + bar_blocks, bar_blocks, dtype=np.int64)
    n_liquidations, _ = np.histogram(blocks, bins=edges)
    end_block = edges[1:]
    end_time_ns = np.interp(end_block, blocks, timestamps_ns).astype(np.int64)

    out = pd.DataFrame(
        {
            "start_block": edges[:-1],
            "end_block": end_block,
            "end_time": pd.to_datetime(end_time_ns, unit="ns", utc=True),
            "n_liquidations": n_liquidations,
        }
    )
    if "amount_usd" in liq_df.columns:
        usd = liq_df["amount_usd"].to_numpy(dtype=float)[order]
        out["liquidated_usd"], _ = np.histogram(
            blocks, bins=edges, weights=np.nan_to_num(usd, nan=0.0)
        )
    return out


def make_bar_labels(
    bars: pd.DataFrame,
    episodes: pd.DataFrame,
    horizon_blocks: int,
    *,
    primary_only: bool = True,
) -> np.ndarray:
    """Binary forecast target, one entry per bar (row order preserved).

    Args:
        bars: feature bars with an `end_block` column (CAS-20).
        episodes: cascade episodes with `start_block` (+ `is_primary`) (CAS-16).
        horizon_blocks: a bar is positive if an episode starts in
            (end_block, end_block + horizon_blocks].
        primary_only: restrict to the primary D-A grid point (ADR-001).

    Returns:
        int8 array of {0, 1}, length == len(bars).
    """
    if horizon_blocks <= 0:
        raise ValueError(f"horizon_blocks must be positive, got {horizon_blocks}")
    eps = episodes[episodes["is_primary"]] if primary_only else episodes
    starts = np.sort(eps["start_block"].to_numpy(dtype=np.int64))
    end_block = bars["end_block"].to_numpy(dtype=np.int64)

    if len(starts) == 0:
        return np.zeros(len(bars), dtype=np.int8)

    # For each bar, is there an episode start in (end_block, end_block + h]?
    # left  = first start strictly greater than end_block
    # right = first start strictly greater than end_block + horizon
    left = np.searchsorted(starts, end_block, side="right")
    right = np.searchsorted(starts, end_block + horizon_blocks, side="right")
    return (right > left).astype(np.int8)


def make_bar_severity_targets(
    bars: pd.DataFrame,
    episodes: pd.DataFrame,
    horizon_blocks: int,
    *,
    primary_only: bool = True,
) -> np.ndarray:
    """Conditional severity-forecast target, one entry per bar (row order
    preserved): the USD severity (`severity_usd`) of the cascade episode
    that begins in (end_block, end_block + horizon_blocks], or 0.0 if none
    does. The magnitude counterpart of `make_bar_labels` -- same horizon
    window, but the target is the realized severity instead of a 0/1
    indicator, for training a severity-forecast head (CAS-19) alongside
    a probability-forecast head.

    Args:
        bars: feature bars with an `end_block` column (CAS-20).
        episodes: cascade episodes with `start_block`, `severity_usd`
            (+ `is_primary`) (CAS-16).
        horizon_blocks: the same forecast window as `make_bar_labels`.
        primary_only: restrict to the primary D-A grid point (ADR-001).

    Returns:
        float64 array, length == len(bars). When multiple episode starts
        fall in one bar's window (rare -- the primary grid point already
        merges overlapping candidate windows, see labels/cascade_labeler.py),
        the nearest (first) one's severity is used, since that is the
        episode this bar's horizon would actually detect first.
    """
    if horizon_blocks <= 0:
        raise ValueError(f"horizon_blocks must be positive, got {horizon_blocks}")
    eps = episodes[episodes["is_primary"]] if primary_only else episodes
    eps = eps.sort_values("start_block")
    starts = eps["start_block"].to_numpy(dtype=np.int64)
    severities = eps["severity_usd"].to_numpy(dtype=float)
    end_block = bars["end_block"].to_numpy(dtype=np.int64)

    out = np.zeros(len(bars), dtype=np.float64)
    if len(starts) == 0:
        return out

    left = np.searchsorted(starts, end_block, side="right")
    right = np.searchsorted(starts, end_block + horizon_blocks, side="right")
    has_match = right > left
    out[has_match] = severities[left[has_match]]
    return out


__all__ = ["build_liquidation_bars", "make_bar_labels", "make_bar_severity_targets"]
