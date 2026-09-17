"""Unit tests for stress-window detection + stress-aware snapshot
cadence."""

from __future__ import annotations

import numpy as np
import pandas as pd

from cascadesignal.graph.stress_windows import (
 detect_stress_windows,
 snapshot_blocks_with_stress,
)


def _bars(liq_usd: list[float], bar_blocks: int = 5) -> pd.DataFrame:
 n = len(liq_usd)
 start = np.arange(n) * bar_blocks
 return pd.DataFrame(
 {
 "start_block": start,
 "end_block": start + bar_blocks,
 "liq_usd": liq_usd,
 }
 )


def test_detect_stress_windows_flags_causal_outlier:
 # 60 quiet bars with mild oscillation (mean ~10, nonzero std) establish
 # a trailing baseline, then one massive spike -- must be flagged as its
 # own single-bar stress window, and must NOT retroactively flag any of
 # the quiet baseline bars.
 quiet = [10.0, 11.0, 9.0] * 20
 spike = [10_000.0]
 tail = [10.0, 11.0, 9.0, 10.0, 11.0]
 bars = _bars(quiet + spike + tail)

 windows = detect_stress_windows(
 bars, window_bars=50, min_periods=20, z_threshold=3.0
 )
 assert len(windows) == 1
 start, end = windows[0]
 spike_start = bars.iloc[60]["start_block"]
 spike_end = bars.iloc[60]["end_block"]
 assert start == spike_start
 assert end == spike_end


def test_detect_stress_windows_empty_bars_returns_no_windows:
 assert (
 detect_stress_windows(
 pd.DataFrame(columns=["start_block", "end_block", "liq_usd"])
 )
 == []
 )


def test_detect_stress_windows_no_outliers_returns_no_windows:
 bars = _bars([10.0] * 40)
 windows = detect_stress_windows(bars, window_bars=20, min_periods=10)
 assert windows == []


def test_snapshot_blocks_with_stress_denser_inside_window:
 blocks = snapshot_blocks_with_stress(
 0, 100, [(40, 60)], normal_cadence=25, stress_cadence=5
 )
 # Dense cadence kicks in as soon as a normal-cadence step from the
 # current anchor would reach into [40, 60) (i.e. from anchor 25 already,
 # not just once an anchor happens to land inside), reverts to coarse
 # once a normal step no longer touches the window (anchor 60 onward).
 assert list(blocks) == [0, 25, 30, 35, 40, 45, 50, 55, 60, 85]


def test_snapshot_blocks_with_stress_narrow_window_not_skipped:
 # Regression for the real production bug: a stress
 # window much narrower than the normal cadence, sitting strictly between
 # two coarse anchors, must still trigger dense sampling on approach
 # the old point-containment check silently skipped straight over it.
 blocks = snapshot_blocks_with_stress(
 0, 500, [(150, 160)], normal_cadence=100, stress_cadence=20
 )
 # The anchor immediately before the window (100) must switch to dense
 # cadence rather than jumping straight to 200, skipping the window
 # entirely -- 120/140 bracket the [150, 160) window tightly instead.
 assert list(blocks) == [0, 100, 120, 140, 160, 260, 360, 460]
 assert 200 not in blocks # the old point-containment bug's coarse jump


def test_snapshot_blocks_with_stress_no_windows_matches_plain_cadence:
 blocks = snapshot_blocks_with_stress(
 0, 100, [], normal_cadence=25, stress_cadence=5
 )
 assert list(blocks) == [0, 25, 50, 75]


def test_snapshot_blocks_with_stress_rejects_bad_args:
 import pytest

 with pytest.raises(ValueError):
 snapshot_blocks_with_stress(100, 50, [])
 with pytest.raises(ValueError):
 snapshot_blocks_with_stress(0, 100, [], normal_cadence=0)
