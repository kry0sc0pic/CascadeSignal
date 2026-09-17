"""Eval-path tests for the Hawkes branching-ratio alarm (E3).

Exercises the walk-forward harness + FAR-budget alerting end to end on
synthetic bars, using the Hawkes model as the scorer (replacing the cut
baseline-driven test_eval_harness). Confirms the eval is leakage-free (seed
bars stay unscored), that a planted liquidation burst before an episode is
detected within its lead window, and that a quiet stream raises no alerts.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from cascadesignal.eval.alerts import (
 classify_alerts,
 raise_alert_events,
 recall_at_far_budget,
)
from cascadesignal.eval.splitter import WalkForwardSplitter
from cascadesignal.models.harness import baseline_report, walk_forward_oof_scores
from cascadesignal.models.hawkes import HawkesUnivariateBranchingRatio
from cascadesignal.models.labels import make_bar_labels

BLOCKS_PER_BAR = 5
BLOCKS_PER_WEEK = 50_400.0


def _bars(marks: np.ndarray, start: str = "2021-01-01") -> pd.DataFrame:
 n = len(marks)
 end_block = np.arange(1, n + 1, dtype=np.int64) * BLOCKS_PER_BAR
 return pd.DataFrame(
 {
 "n_liquidations": marks.astype(float),
 "start_block": end_block - BLOCKS_PER_BAR,
 "end_block": end_block,
 "end_time": pd.date_range(start, periods=n, freq="h", tz="UTC"),
 }
 )


def _episode(bars: pd.DataFrame, at_bar: int, span_bars: int = 3) -> pd.DataFrame:
 """A single primary episode starting at `at_bar`."""
 return pd.DataFrame(
 {
 "start_block": [int(bars["end_block"].iloc[at_bar])],
 "end_block": [int(bars["end_block"].iloc[at_bar + span_bars])],
 "start_time": [bars["end_time"].iloc[at_bar]],
 "is_primary": [True],
 "severity_usd": [1.0e7],
 }
 )


def test_make_bar_labels_marks_only_pre_episode_horizon:
 bars = _bars(np.zeros(100))
 ep = _episode(bars, at_bar=60)
 # horizon of 5 bars = 25 blocks: bars whose (end_block, end_block+25] spans
 # the episode start get a 1.
 y = make_bar_labels(bars, ep, horizon_blocks=25, primary_only=True)
 assert y.sum > 0
 # No bar at or after the episode start is labelled positive.
 assert y[60:].sum == 0


def test_walk_forward_scores_are_out_of_fold_and_seed_stays_unscored:
 rng = np.random.default_rng(0)
 marks = rng.poisson(0.05, size=1500).astype(float)
 bars = _bars(marks)
 # Monthly folds over ~2 months of hourly bars, 1-month seed.
 bars["end_time"] = pd.date_range(
 "2021-01-01", periods=len(bars), freq="h", tz="UTC"
 )
 ep = _episode(bars, at_bar=1200)
 y = make_bar_labels(bars, ep, horizon_blocks=50, primary_only=True)
 splitter = WalkForwardSplitter(min_train_months=1)
 oof, scored = walk_forward_oof_scores(
 HawkesUnivariateBranchingRatio, bars, y, splitter
 )
 assert scored.any and not scored.all # seed month excluded
 s = oof[scored]
 assert np.isfinite(s).all
 assert ((s >= 0) & (s < 1)).all # branching ratio bounded


def test_burst_before_episode_is_detected_within_lead_window:
 marks = np.zeros(600)
 marks[299:304] = 20.0 # burst just before the episode start
 bars = _bars(marks)
 ep = _episode(bars, at_bar=305)
 lead_blocks = 40 # 8 bars

 model = HawkesUnivariateBranchingRatio.fit(bars)
 scores = model.score(bars)
 blocks = bars["end_block"].to_numpy(dtype=np.int64)

 res = recall_at_far_budget(scores, blocks, ep, lead_blocks, 1.0, BLOCKS_PER_WEEK)
 alert_blocks = raise_alert_events(scores, blocks, res.threshold)
 _, detected = classify_alerts(alert_blocks, ep, lead_blocks)
 assert 0 in detected # the one episode is caught


def test_quiet_stream_raises_no_true_detections:
 bars = _bars(np.zeros(400))
 model = HawkesUnivariateBranchingRatio.fit(bars)
 scores = model.score(bars)
 blocks = bars["end_block"].to_numpy(dtype=np.int64)
 # A high threshold on an all-quiet stream yields no rising-edge alerts.
 alert_blocks = raise_alert_events(scores, blocks, threshold=0.5)
 assert len(alert_blocks) == 0


def test_baseline_report_lift_over_prevalence_on_hawkes:
 rng = np.random.default_rng(3)
 marks = rng.poisson(0.05, size=1500).astype(float)
 marks[900:905] += 25 # one burst -> one detectable episode
 bars = _bars(marks)
 bars["end_time"] = pd.date_range(
 "2021-01-01", periods=len(bars), freq="h", tz="UTC"
 )
 ep = _episode(bars, at_bar=908)
 y = make_bar_labels(bars, ep, horizon_blocks=50, primary_only=True)
 splitter = WalkForwardSplitter(min_train_months=1)
 oof, scored = walk_forward_oof_scores(
 HawkesUnivariateBranchingRatio, bars, y, splitter
 )
 rep = baseline_report("Hawkes n(t)", oof, scored, y, bars, ep, [50])
 assert rep["n_scored"] > 0
 assert rep["prevalence"] > 0
 # If AUPRC is computable, lift over prevalence should be reported.
 if rep["auprc"] is not None:
 assert rep["auprc_lift_over_prevalence"] is not None
