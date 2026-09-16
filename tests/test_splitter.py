"""Unit tests for the walk-forward splitter + episode hold-outs (CAS-35).

The load-bearing test is no-look-ahead (T6): every fold's train times must be
strictly before its test times. The rest pin the monthly cadence, the expanding
train window, the episode hold-outs, and the FAR-budget accounting. Synthetic
frames only -- no data lake needed, runs in CI.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cascadesignal.eval.splitter import (
    EPISODE_HOLDOUTS,
    WalkForwardSplitter,
    far_budget,
    weeks_in_span,
    within_far_budget,
)


def _frame(
    start: str = "2021-01-01", periods: int = 400, freq: str = "D"
) -> pd.DataFrame:
    times = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    return pd.DataFrame({"end_time": times, "row": np.arange(periods)})


def test_no_lookahead_every_fold():
    df = _frame()
    splitter = WalkForwardSplitter(min_train_months=3)
    folds = list(splitter.split(df))
    assert folds
    times = df["end_time"]
    for fold in folds:
        train_max = times.iloc[fold.train_idx].max()
        test_min = times.iloc[fold.test_idx].min()
        assert train_max < test_min, f"leakage in {fold.name}"
        assert train_max < fold.test_start


def test_monthly_cadence_and_single_month_test():
    df = _frame(periods=400)
    folds = list(WalkForwardSplitter(min_train_months=3).split(df))
    for fold in folds:
        assert fold.test_end == fold.test_start + pd.offsets.MonthBegin(1)
        test_times = df["end_time"].iloc[fold.test_idx]
        assert (test_times >= fold.test_start).all()
        assert (test_times < fold.test_end).all()
    # consecutive folds advance by one month
    starts = [f.test_start for f in folds]
    for a, b in zip(starts, starts[1:]):
        assert b == a + pd.offsets.MonthBegin(1)


def test_min_train_months_seed_period():
    df = _frame(start="2021-01-01", periods=400)
    folds = list(WalkForwardSplitter(min_train_months=6).split(df))
    # First test month is the 7th month (index 6) -> 2021-07.
    assert folds[0].test_start == pd.Timestamp("2021-07-01", tz="UTC")


def test_expanding_train_window_is_a_prefix():
    df = _frame()
    folds = list(WalkForwardSplitter(min_train_months=3).split(df))
    sizes = [len(f.train_idx) for f in folds]
    assert sizes == sorted(sizes)  # non-decreasing
    for fold in folds:
        # train indices are exactly the rows before test_start, in order
        expected = np.where((df["end_time"] < fold.test_start).to_numpy())[0]
        assert np.array_equal(fold.train_idx, expected)
        assert set(fold.train_idx).isdisjoint(fold.test_idx)


def test_episode_holdouts_train_before_test():
    # Frame spanning all four hold-out episodes.
    df = _frame(start="2021-06-01", periods=1800)  # ~ into 2026
    folds = WalkForwardSplitter().episode_holdout_folds(df)
    names = {ho.name for ho in EPISODE_HOLDOUTS}
    got = {EPISODE_HOLDOUTS[f.fold_id].name for f in folds}
    assert got  # at least some hold-outs have data
    for fold in folds:
        train_max = df["end_time"].iloc[fold.train_idx].max()
        test_min = df["end_time"].iloc[fold.test_idx].min()
        assert train_max < test_min
        assert train_max < fold.train_end
    assert got.issubset(names)


def test_episode_holdout_skipped_when_no_test_rows():
    # Data ends in 2022 -> only terra/ftx hold-outs have test rows.
    df = _frame(start="2021-06-01", periods=500)  # ends ~ Oct 2022
    folds = WalkForwardSplitter().episode_holdout_folds(df)
    got = {EPISODE_HOLDOUTS[f.fold_id].name for f in folds}
    assert "oct_2025" not in got and "feb_2026" not in got


def test_far_budget_math():
    start = pd.Timestamp("2022-01-01", tz="UTC")
    end = pd.Timestamp("2022-01-29", tz="UTC")  # 4 weeks
    assert weeks_in_span(start, end) == pytest.approx(4.0)
    assert far_budget(start, end, budget_per_week=1.0) == pytest.approx(4.0)
    assert within_far_budget(4, start, end, 1.0)
    assert not within_far_budget(5, start, end, 1.0)


def test_missing_time_col_and_nat_raise():
    with pytest.raises(KeyError):
        list(WalkForwardSplitter(time_col="nope").split(_frame()))
    bad = _frame(periods=10)
    bad.loc[0, "end_time"] = pd.NaT
    with pytest.raises(ValueError, match="NaT"):
        list(WalkForwardSplitter(min_train_months=1).split(bad))
