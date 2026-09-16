"""Unit tests for `build_liquidation_bars` (Hawkes-EWS multi-protocol expansion)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from cascadesignal.models.labels import build_liquidation_bars


def _liq_df(blocks: list[int]) -> pd.DataFrame:
    t0 = pd.Timestamp("2021-01-01T00:00:00Z")
    return pd.DataFrame(
        {
            "block_number": blocks,
            "block_timestamp": [t0 + pd.Timedelta(seconds=12 * b) for b in blocks],
        }
    )


def test_bin_counts_match_a_hand_computed_histogram():
    # bar_blocks=5 over blocks [0, 20] -> edges [0,5,10,15,20], 4 bars.
    liq = _liq_df([0, 0, 12, 12, 12, 20])
    bars = build_liquidation_bars(liq, bar_blocks=5)

    assert list(bars["start_block"]) == [0, 5, 10, 15]
    assert list(bars["end_block"]) == [5, 10, 15, 20]
    assert list(bars["n_liquidations"]) == [2, 0, 3, 1]
    assert bars["n_liquidations"].sum() == len(liq)


def test_empty_bars_still_get_an_interpolated_end_time():
    liq = _liq_df([0, 0, 12, 12, 12, 20])
    bars = build_liquidation_bars(liq, bar_blocks=5)

    empty_bar = bars.iloc[1]  # [5, 10), no liquidations
    assert empty_bar["n_liquidations"] == 0
    t0 = pd.Timestamp("2021-01-01T00:00:00Z")
    expected = t0 + pd.Timedelta(seconds=120)  # interp(10, [0,12], [0s,144s])
    assert abs((empty_bar["end_time"] - expected).total_seconds()) < 1e-6

    # end_time is non-decreasing across bars and stays within the observed
    # timestamp range (interpolation, never extrapolation, inside the span).
    assert bars["end_time"].is_monotonic_increasing
    assert bars["end_time"].min() >= liq["block_timestamp"].min()
    assert bars["end_time"].max() <= liq["block_timestamp"].max()


def test_bar_blocks_controls_bin_width():
    liq = _liq_df([0, 3, 6, 9])
    bars = build_liquidation_bars(liq, bar_blocks=3)

    assert list(bars["start_block"]) == [0, 3, 6]
    assert list(bars["end_block"]) == [3, 6, 9]
    assert list(bars["n_liquidations"]) == [1, 1, 2]


def test_covers_full_block_range_with_no_gaps():
    rng = np.random.default_rng(0)
    blocks = np.sort(rng.integers(0, 10_000, size=500))
    liq = _liq_df(blocks.tolist())
    bars = build_liquidation_bars(liq, bar_blocks=5)

    assert (
        bars["start_block"].to_numpy()[1:] == bars["end_block"].to_numpy()[:-1]
    ).all()
    assert bars["start_block"].iloc[0] == blocks.min()
    assert bars["end_block"].iloc[-1] >= blocks.max()
    assert bars["n_liquidations"].sum() == len(blocks)
