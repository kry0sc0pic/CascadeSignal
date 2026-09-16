"""Event-based alert evaluation (CAS-36): recall @ fixed false-alarm budget,
lead-time frontier.

These operate on a continuous risk-score stream (one score per evaluation
block/bar) and cascade episodes (CAS-16 `data/curated/labels/*/episodes.parquet`
schema: `start_block`, `end_block`). Definitions, since the spec leaves the
exact accounting open (CAS-36 subtask: "Define 'protectable USD' accounting
precisely" -- the same precision is needed here first):

- An **alert** is a discrete event: a rising-edge crossing of `threshold` in
  the score stream (the first block of a contiguous above-threshold run).
  Sustained high scores during one episode count as one alert, not one per
  block -- matching the "≤ 1 alert/week" budget framing, which is about
  distinct raised flags, not raw block counts.
- An alert is a **true detection** of episode `e` if it falls in the lead
  window `[e.start_block - lead_blocks, e.start_block)` -- i.e. it fires
  with at least `lead_blocks` of warning before the cascade starts. Alerts
  firing during or after an episode do not count: by definition an "early
  warning" system that fires late failed at the one thing it needs to do.
- An alert is a **false alarm** if it falls in none of the episodes' lead
  windows and none of the episodes' [start_block, end_block] spans (i.e. it
  fires during a genuinely quiet period).
- Precision = (# alerts that are true detections) / (# alerts). Multiple
  alerts landing in the same episode's lead window each count toward
  precision's numerator (they were each "not wrong"), but only one true
  detection is credited toward recall for that episode.
- Recall = (# episodes with >=1 alert in their lead window) / (# episodes).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def raise_alert_events(
    scores: np.ndarray, blocks: np.ndarray, threshold: float
) -> np.ndarray:
    """Return the block number of each rising-edge threshold crossing.

    `blocks` must be sorted ascending and aligned 1:1 with `scores`.
    """
    scores = np.asarray(scores, dtype=float)
    blocks = np.asarray(blocks, dtype=np.int64)
    above = scores >= threshold
    rising = above & ~np.concatenate(([False], above[:-1]))
    return blocks[rising]


def _lead_windows(
    episodes: pd.DataFrame, lead_blocks: int
) -> list[tuple[int, int, int]]:
    """Return (episode_index, window_start, window_end) for each episode's
    lead window [start_block - lead_blocks, start_block)."""
    starts = episodes["start_block"].to_numpy(dtype=np.int64)
    return [(i, int(s) - lead_blocks, int(s)) for i, s in enumerate(starts)]


def _in_any_span(block: int, spans: list[tuple[int, int]]) -> bool:
    return any(lo <= block < hi for lo, hi in spans)


def classify_alerts(
    alert_blocks: np.ndarray, episodes: pd.DataFrame, lead_blocks: int
) -> tuple[pd.DataFrame, set[int]]:
    """Classify each alert as a true detection (of which episode) or a false
    alarm. Returns (per-alert DataFrame, set of detected episode indices)."""
    lead_windows = _lead_windows(episodes, lead_blocks)
    starts = episodes["start_block"].to_numpy(dtype=np.int64)
    ends = episodes["end_block"].to_numpy(dtype=np.int64)
    episode_spans = list(zip(starts.tolist(), (ends + 1).tolist()))

    rows = []
    detected: set[int] = set()
    for block in alert_blocks:
        matched_episode = None
        for ep_idx, lo, hi in lead_windows:
            if lo <= block < hi:
                matched_episode = ep_idx
                detected.add(ep_idx)
                break
        is_quiet = matched_episode is None and not _in_any_span(block, episode_spans)
        rows.append(
            {
                "block": int(block),
                "is_true_detection": matched_episode is not None,
                "episode_index": matched_episode,
                "is_false_alarm": is_quiet,
            }
        )
    return (
        pd.DataFrame(
            rows,
            columns=["block", "is_true_detection", "episode_index", "is_false_alarm"],
        ),
        detected,
    )


def precision_recall_at_threshold(
    scores: np.ndarray,
    blocks: np.ndarray,
    episodes: pd.DataFrame,
    threshold: float,
    lead_blocks: int,
) -> dict:
    """Precision/recall for a single alert threshold (see module docstring
    for the exact accounting)."""
    alert_blocks = raise_alert_events(scores, blocks, threshold)
    if len(episodes) == 0:
        raise ValueError("episodes must be non-empty")
    classified, detected = classify_alerts(alert_blocks, episodes, lead_blocks)

    n_alerts = len(classified)
    n_true = int(classified["is_true_detection"].sum())
    precision = n_true / n_alerts if n_alerts else 0.0
    recall = len(detected) / len(episodes)
    return {
        "threshold": threshold,
        "n_alerts": n_alerts,
        "n_true_detections": n_true,
        "n_false_alarms": int(classified["is_false_alarm"].sum()),
        "precision": precision,
        "recall": recall,
    }


def false_alarm_rate_per_week(
    scores: np.ndarray,
    blocks: np.ndarray,
    episodes: pd.DataFrame,
    threshold: float,
    lead_blocks: int,
    blocks_per_week: float,
) -> float:
    """False alarms per week over the full evaluated block range."""
    alert_blocks = raise_alert_events(scores, blocks, threshold)
    classified, _ = classify_alerts(alert_blocks, episodes, lead_blocks)
    n_false = int(classified["is_false_alarm"].sum())
    total_weeks = (blocks.max() - blocks.min()) / blocks_per_week
    if total_weeks <= 0:
        raise ValueError("block range must span at least one week")
    return n_false / total_weeks


@dataclass(frozen=True)
class FarBudgetResult:
    threshold: float | None
    recall: float
    achieved_far_per_week: float
    feasible: bool


def recall_at_far_budget(
    scores: np.ndarray,
    blocks: np.ndarray,
    episodes: pd.DataFrame,
    lead_blocks: int,
    far_budget_per_week: float,
    blocks_per_week: float,
    thresholds: np.ndarray | None = None,
) -> FarBudgetResult:
    """Sweep thresholds; among those meeting the false-alarm budget, return
    the one with the best recall. `feasible=False` if no threshold in the
    sweep meets the budget (reports the lowest-FAR threshold instead)."""
    if thresholds is None:
        thresholds = np.unique(scores)

    best: FarBudgetResult | None = None
    lowest_far: FarBudgetResult | None = None
    for t in thresholds:
        far = false_alarm_rate_per_week(
            scores, blocks, episodes, t, lead_blocks, blocks_per_week
        )
        pr = precision_recall_at_threshold(scores, blocks, episodes, t, lead_blocks)
        candidate = FarBudgetResult(
            threshold=float(t),
            recall=pr["recall"],
            achieved_far_per_week=far,
            feasible=True,
        )
        if lowest_far is None or far < lowest_far.achieved_far_per_week:
            lowest_far = candidate
        if far <= far_budget_per_week and (best is None or pr["recall"] > best.recall):
            best = candidate

    if best is not None:
        return best
    assert lowest_far is not None  # thresholds is non-empty by construction
    return FarBudgetResult(
        threshold=lowest_far.threshold,
        recall=lowest_far.recall,
        achieved_far_per_week=lowest_far.achieved_far_per_week,
        feasible=False,
    )


def lead_time_frontier(
    scores: np.ndarray,
    blocks: np.ndarray,
    episodes: pd.DataFrame,
    h_values: list[int],
    precision_floor: float,
    thresholds: np.ndarray | None = None,
) -> pd.DataFrame:
    """For each lead time h in `h_values`, sweep thresholds and report the
    best recall achievable at precision >= `precision_floor`. The lead-time
    frontier (PLAN §7 E6) is the max h with a feasible row."""
    if thresholds is None:
        thresholds = np.unique(scores)

    rows = []
    for h in h_values:
        best_recall = 0.0
        best_precision = None
        feasible = False
        for t in thresholds:
            pr = precision_recall_at_threshold(scores, blocks, episodes, t, h)
            if pr["precision"] >= precision_floor and pr["recall"] > best_recall:
                best_recall = pr["recall"]
                best_precision = pr["precision"]
                feasible = True
        rows.append(
            {
                "lead_blocks": h,
                "feasible": feasible,
                "best_recall": best_recall if feasible else None,
                "precision_at_best_recall": best_precision,
            }
        )
    return pd.DataFrame(rows)
