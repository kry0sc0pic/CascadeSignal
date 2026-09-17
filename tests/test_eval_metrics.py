"""T6 hand-computed-fixture tests for the."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cascadesignal.eval.alerts import (
 classify_alerts,
 lead_time_frontier,
 precision_recall_at_threshold,
 raise_alert_events,
 recall_at_far_budget,
)
from cascadesignal.eval.economics import usd_protectable
from cascadesignal.eval.metrics import (
 auprc,
 brier_score,
 calibration_curve,
 quantile_loss,
)


def test_auprc_matches_hand_computed_value:
 # Well-known 4-point example: AP = 0.8333...
 y_true = np.array([0, 0, 1, 1])
 y_score = np.array([0.1, 0.4, 0.35, 0.8])
 assert auprc(y_true, y_score) == pytest.approx(0.8333333333333333)


def test_brier_score_matches_hand_computed_value:
 y_true = np.array([0, 1, 1, 0])
 y_score = np.array([0.1, 0.9, 0.8, 0.3])
 # mean((0.1-0)^2, (0.9-1)^2, (0.8-1)^2, (0.3-0)^2) = mean(.01,.01,.04,.09) = .0375
 assert brier_score(y_true, y_score) == pytest.approx(0.0375)


def test_calibration_curve_matches_hand_computed_bins:
 y_true = [0, 1, 1, 1]
 y_score = [0.1, 0.2, 0.6, 0.9]
 df = calibration_curve(y_true, y_score, n_bins=2)
 assert df["mean_predicted"].tolist == pytest.approx([0.15, 0.75])
 assert df["observed_frequency"].tolist == pytest.approx([0.5, 1.0])


def test_quantile_loss_median_equals_half_mae:
 y_true = np.array([10.0, 20.0])
 y_pred = np.array([12.0, 18.0])
 # errors -2, +2 -> pinball@0.5 = 0.5 * mean(|error|) = 0.5 * 2 = 1.0
 assert quantile_loss(y_true, y_pred, 0.5) == pytest.approx(1.0)


def test_quantile_loss_rejects_invalid_quantile:
 with pytest.raises(ValueError):
 quantile_loss([1.0], [1.0], 1.5)


# ---------------------------------------------------------------------------
# Event-based alerting
# ---------------------------------------------------------------------------


def test_raise_alert_events_detects_rising_edges_only:
 scores = np.array([0.1, 0.6, 0.7, 0.2, 0.9])
 blocks = np.array([10, 20, 30, 40, 50])
 alerts = raise_alert_events(scores, blocks, threshold=0.5)
 # block 20 rises above; block 30 stays above (not a new alert);
 # block 40 drops below; block 50 rises again.
 assert list(alerts) == [20, 50]


@pytest.fixture
def single_episode -> pd.DataFrame:
 return pd.DataFrame(
 {"start_block": [100], "end_block": [110], "severity_usd": [1_000_000.0]}
 )


def test_classify_alerts_true_detection_false_alarm_and_too_late(single_episode):
 # 50: quiet (outside lead window [80,100) and outside span [100,110]) -> false alarm
 # 90: inside lead window [80,100) -> true detection
 # 105: inside episode span [100,110] but not lead window -> neither (too late)
 classified, detected = classify_alerts(
 alert_blocks=np.array([50, 90, 105]), episodes=single_episode, lead_blocks=20
 )
 assert detected == {0}
 row = {r.block: r for r in classified.itertuples}
 assert row[50].is_false_alarm and not row[50].is_true_detection
 assert row[90].is_true_detection and not row[90].is_false_alarm
 assert not row[105].is_true_detection and not row[105].is_false_alarm


def _spike_series(
 spike_blocks: list[int], total_blocks: int = 121
) -> tuple[np.ndarray, np.ndarray]:
 blocks = np.arange(total_blocks)
 scores = np.zeros(total_blocks)
 scores[spike_blocks] = 1.0
 return scores, blocks


def test_precision_recall_at_threshold_matches_hand_computed(single_episode):
 scores, blocks = _spike_series([50, 90, 105])
 result = precision_recall_at_threshold(
 scores, blocks, single_episode, threshold=1.0, lead_blocks=20
 )
 assert result["n_alerts"] == 3
 assert result["n_true_detections"] == 1
 assert result["n_false_alarms"] == 1
 assert result["precision"] == pytest.approx(1 / 3)
 assert result["recall"] == pytest.approx(1.0)


def test_recall_at_far_budget_matches_hand_computed(single_episode):
 scores, blocks = _spike_series([50, 90, 105])
 # 121 blocks / 20 blocks-per-week = 6 weeks; 1 false alarm -> FAR ~= 0.1667/week
 result = recall_at_far_budget(
 scores,
 blocks,
 single_episode,
 lead_blocks=20,
 far_budget_per_week=0.2,
 blocks_per_week=20.0,
 )
 assert result.feasible
 assert result.threshold == pytest.approx(1.0)
 assert result.recall == pytest.approx(1.0)
 assert result.achieved_far_per_week == pytest.approx(1 / 6)


def test_lead_time_frontier_matches_hand_computed(single_episode):
 scores, blocks = _spike_series([50, 90, 105])
 df = lead_time_frontier(
 scores, blocks, single_episode, h_values=[20, 5], precision_floor=0.3
 )
 by_h = df.set_index("lead_blocks")
 assert by_h.loc[20, "feasible"]
 assert by_h.loc[20, "best_recall"] == pytest.approx(1.0)
 assert by_h.loc[20, "precision_at_best_recall"] == pytest.approx(1 / 3)
 # narrower window excludes block 90 (not in [95,100)) -> nothing detected
 assert not by_h.loc[5, "feasible"]
 assert pd.isna(by_h.loc[5, "best_recall"])


# ---------------------------------------------------------------------------
# Economic USD-protectable metric
# ---------------------------------------------------------------------------


def test_usd_protectable_point_estimate_matches_hand_computed:
 episodes = pd.DataFrame(
 {
 "start_block": [100, 300],
 "end_block": [110, 310],
 "severity_usd": [1_000_000.0, 3_000_000.0],
 }
 )
 scores, blocks = _spike_series([90], total_blocks=320)
 result = usd_protectable(
 scores, blocks, episodes, threshold=1.0, lead_blocks=20, n_bootstrap=200
 )
 assert result.n_episodes == 2
 assert result.n_protected == 1
 # only episode 0 detected (block 90 in [80,100)); episode 1's window [280,300) untouched
 assert result.point_estimate == pytest.approx(1_000_000.0)
 assert result.ci_low <= result.point_estimate <= result.ci_high
