"""Walk-forward splitter + episode hold-outs.

The evaluation backbone every experiment runs through (PLAN §7): strict
expanding-window walk-forward with a monthly retrain cadence and zero
look-ahead, plus the four fixed episode hold-outs, plus the quiet-period
false-alarm budget accounting.

Zero look-ahead is structural, not incidental: a fold's train set is every row
strictly before the fold's `test_start`, so `max(train_time) < min(test_time)`
always. The leakage test (T6) checks exactly this.

Splits are keyed on a timestamp column (default `end_time`, the feature bar's
as-of cutoff from) because the retrain cadence and the episode hold-out
cutoffs are calendar-defined. The block-defined golden-episode windows live in
configs/ingest.yaml / docs/episodes; the hold-out spans below are their eval
counterparts, anchored to the PLAN §7 train cutoffs.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

_ONE_WEEK = pd.Timedelta(weeks=1)


@dataclass(frozen=True)
class Fold:
 """One walk-forward fold. `train_idx`/`test_idx` are positional indices
 into the frame passed to the splitter (row order preserved)."""

 fold_id: int
 train_idx: np.ndarray
 test_idx: np.ndarray
 train_end: pd.Timestamp # exclusive upper bound of train (== test_start)
 test_start: pd.Timestamp # inclusive
 test_end: pd.Timestamp # exclusive

 @property
 def name(self) -> str:
 return f"fold_{self.fold_id:03d}_{self.test_start.date}"


@dataclass(frozen=True)
class EpisodeHoldout:
 """A fixed hold-out: train on everything before `train_end`, test on the
 episode span [test_start, test_end)."""

 name: str
 train_end: pd.Timestamp
 test_start: pd.Timestamp
 test_end: pd.Timestamp


def _ts(value: str) -> pd.Timestamp:
 return pd.Timestamp(value, tz="UTC")


# PLAN §7: "train pre-Apr'22 -> test Terra; pre-Oct'22 -> FTX; pre-Sep'25 ->
# Oct'25; pre-Jan'26 -> Feb'26." Test spans are generous windows bracketing
# each golden episode (canonical block windows: configs/ingest.yaml
# golden_episodes / docs/episodes). China (2021-05) is intentionally NOT a
# hold-out -- it always stays in training, per PLAN §7.
EPISODE_HOLDOUTS: tuple[EpisodeHoldout, ...] = (
 EpisodeHoldout("terra", _ts("2022-04-01"), _ts("2022-05-07"), _ts("2022-06-21")),
 EpisodeHoldout("ftx", _ts("2022-10-01"), _ts("2022-11-06"), _ts("2022-11-15")),
 EpisodeHoldout("oct_2025", _ts("2025-09-01"), _ts("2025-10-06"), _ts("2025-10-14")),
 EpisodeHoldout("feb_2026", _ts("2026-01-01"), _ts("2026-01-31"), _ts("2026-02-07")),
)


class WalkForwardSplitter:
 """Expanding-window, monthly-retrain walk-forward splitter."""

 def __init__(self, time_col: str = "end_time", min_train_months: int = 6):
 if min_train_months < 1:
 raise ValueError("min_train_months must be >= 1")
 self.time_col = time_col
 self.min_train_months = min_train_months

 def _times(self, df: pd.DataFrame) -> pd.Series:
 if self.time_col not in df.columns:
 raise KeyError(f"time_col {self.time_col!r} not in frame")
 times = pd.to_datetime(df[self.time_col], utc=True)
 if times.isna.any:
 raise ValueError(
 f"{self.time_col} has NaT values -- drop or fill them before splitting"
 )
 return times

 def split(self, df: pd.DataFrame) -> Iterator[Fold]:
 """Yield expanding-window folds, one per calendar month of test data.

 Train = all rows strictly before the test month's start; test = rows in
 that month. The first `min_train_months` of data seed the initial train
 set and are never a test month.
 """
 times = self._times(df)
 pos = np.arange(len(df))
 # Month-start boundaries spanning the data.
 first_month = times.min.normalize.replace(day=1)
 month_starts = pd.date_range(
 start=first_month,
 end=times.max.normalize.replace(day=1),
 freq="MS",
 tz="UTC",
 )
 fold_id = 0
 for i, test_start in enumerate(month_starts):
 if i < self.min_train_months:
 continue # seed period -- part of train only
 test_end = test_start + pd.offsets.MonthBegin(1)
 train_mask = (times < test_start).to_numpy
 test_mask = ((times >= test_start) & (times < test_end)).to_numpy
 if not test_mask.any or not train_mask.any:
 continue
 yield Fold(
 fold_id=fold_id,
 train_idx=pos[train_mask],
 test_idx=pos[test_mask],
 train_end=test_start,
 test_start=test_start,
 test_end=test_end,
 )
 fold_id += 1

 def episode_holdout_folds(
 self,
 df: pd.DataFrame,
 holdouts: Sequence[EpisodeHoldout] = EPISODE_HOLDOUTS,
 ) -> list[Fold]:
 """One Fold per configured episode hold-out (train before `train_end`,
 test on the episode span). Hold-outs whose test span has no rows in
 `df` are skipped (e.g. a protocol whose data doesn't reach that date)."""
 times = self._times(df)
 pos = np.arange(len(df))
 folds: list[Fold] = []
 for i, ho in enumerate(holdouts):
 train_mask = (times < ho.train_end).to_numpy
 test_mask = ((times >= ho.test_start) & (times < ho.test_end)).to_numpy
 if not test_mask.any or not train_mask.any:
 continue
 folds.append(
 Fold(
 fold_id=i,
 train_idx=pos[train_mask],
 test_idx=pos[test_mask],
 train_end=ho.train_end,
 test_start=ho.test_start,
 test_end=ho.test_end,
 )
 )
 return folds


def weeks_in_span(start: pd.Timestamp, end: pd.Timestamp) -> float:
 """Number of weeks in [start, end)."""
 delta = pd.Timestamp(end) - pd.Timestamp(start)
 if delta <= pd.Timedelta(0):
 raise ValueError("end must be after start")
 return delta / _ONE_WEEK


def far_budget(
 start: pd.Timestamp, end: pd.Timestamp, budget_per_week: float = 1.0
) -> float:
 """Max false alarms permitted over [start, end) at `budget_per_week`
 (PLAN §7 quiet-period budget, default <= 1 alert/week)."""
 return weeks_in_span(start, end) * budget_per_week


def within_far_budget(
 n_false_alarms: int,
 start: pd.Timestamp,
 end: pd.Timestamp,
 budget_per_week: float = 1.0,
) -> bool:
 """True iff `n_false_alarms` over [start, end) is within the FAR budget."""
 return n_false_alarms <= far_budget(start, end, budget_per_week)


__all__ = [
 "Fold",
 "EpisodeHoldout",
 "EPISODE_HOLDOUTS",
 "WalkForwardSplitter",
 "weeks_in_span",
 "far_budget",
 "within_far_budget",
]
