"""Walk-forward baseline harness (CAS-24 / E2).

Runs any model with a fit(X, y)->self / score(X)->np.ndarray interface (see
models/hawkes.py) through the CAS-35 walk-forward splitter to produce
out-of-fold (OOF) scores, then reports the AUPRC floor and the lead-time
frontier against the CAS-16 cascade episodes.

OOF scoring is what makes the reported AUPRC honest: every bar is scored only
by a model trained on strictly-earlier folds (the splitter guarantees
`max(train_time) < min(test_time)`), so there is no in-sample optimism.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from cascadesignal.eval.alerts import lead_time_frontier
from cascadesignal.eval.metrics import auprc
from cascadesignal.eval.splitter import WalkForwardSplitter

# A baseline is anything with fit(X, y)->self and score(X)->np.ndarray.
ModelFactory = Callable[[], object]


def walk_forward_oof_scores(
    model_factory: ModelFactory,
    bars: pd.DataFrame,
    y: np.ndarray,
    splitter: WalkForwardSplitter,
) -> tuple[np.ndarray, np.ndarray]:
    """Out-of-fold scores over the walk-forward folds.

    Returns (oof_scores, scored_mask). Bars never in any test fold (the seed
    period) stay NaN and are excluded via `scored_mask`.
    """
    oof = np.full(len(bars), np.nan, dtype=float)
    for fold in splitter.split(bars):
        model = model_factory()
        model.fit(bars.iloc[fold.train_idx], y[fold.train_idx])  # type: ignore[attr-defined]
        oof[fold.test_idx] = model.score(bars.iloc[fold.test_idx])  # type: ignore[attr-defined]
    return oof, ~np.isnan(oof)


def _threshold_grid(scores: np.ndarray, n: int = 200) -> np.ndarray:
    """A coarse ascending threshold grid from score quantiles (keeps the
    frontier sweep cheap on millions of bars)."""
    finite = scores[np.isfinite(scores)]
    if finite.size == 0:
        return np.array([0.0])
    qs = np.linspace(0.0, 1.0, n)
    return np.unique(np.quantile(finite, qs))


def baseline_report(
    name: str,
    oof: np.ndarray,
    scored_mask: np.ndarray,
    y: np.ndarray,
    bars: pd.DataFrame,
    episodes: pd.DataFrame,
    h_values: list[int],
    precision_floor: float = 0.5,
) -> dict:
    """AUPRC floor + lead-time frontier for one baseline's OOF scores.

    AUPRC is over the scored (non-seed) bars only; the lead-time frontier is
    over the same scored stream vs. the primary cascade episodes.
    """
    scores = oof[scored_mask]
    labels = y[scored_mask]
    blocks = bars["end_block"].to_numpy(dtype=np.int64)[scored_mask]
    prevalence = float(labels.mean()) if labels.size else 0.0

    result: dict = {
        "baseline": name,
        "n_scored": int(scored_mask.sum()),
        "n_positive": int(labels.sum()),
        "prevalence": prevalence,
    }

    # AUPRC needs both classes present among scored bars.
    if labels.sum() > 0 and labels.sum() < labels.size:
        result["auprc"] = auprc(labels, scores)
        result["auprc_lift_over_prevalence"] = (
            result["auprc"] / prevalence if prevalence > 0 else None
        )
    else:
        result["auprc"] = None

    primary = episodes[episodes["is_primary"]]
    # Restrict to episodes whose start falls inside the scored block range.
    lo, hi = blocks.min(), blocks.max()
    primary = primary[(primary["start_block"] >= lo) & (primary["start_block"] <= hi)]
    if len(primary) > 0:
        grid = _threshold_grid(scores)
        frontier = lead_time_frontier(
            scores, blocks, primary, h_values, precision_floor, thresholds=grid
        )
        result["n_episodes_in_range"] = int(len(primary))
        result["lead_time_frontier"] = frontier.to_dict(orient="records")
        feasible = frontier[frontier["feasible"]]
        result["max_feasible_lead_blocks"] = (
            int(feasible["lead_blocks"].max()) if len(feasible) else None
        )
    else:
        result["n_episodes_in_range"] = 0
        result["lead_time_frontier"] = []
        result["max_feasible_lead_blocks"] = None

    return result


__all__ = ["walk_forward_oof_scores", "baseline_report"]
