"""Probabilistic/pointwise metrics: AUPRC, Brier, calibration,
severity quantile loss.

AUPRC is the primary metric (PLAN §7) given severe class imbalance -- cascade
blocks are rare relative to quiet blocks, so ROC-AUC would be misleadingly
high even for a weak model.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.calibration import calibration_curve as _sk_calibration_curve
from sklearn.metrics import average_precision_score, brier_score_loss


def auprc(y_true: np.ndarray, y_score: np.ndarray) -> float:
 """Area under the precision-recall curve. Primary metric (PLAN §7)."""
 return float(average_precision_score(y_true, y_score))


def brier_score(y_true: np.ndarray, y_score: np.ndarray) -> float:
 """Mean squared error between predicted probability and outcome."""
 return float(brier_score_loss(y_true, y_score))


def calibration_curve(
 y_true: np.ndarray, y_score: np.ndarray, n_bins: int = 10
) -> pd.DataFrame:
 """Reliability diagram data: mean predicted probability vs. observed
 frequency per bin of predicted probability."""
 observed, predicted = _sk_calibration_curve(
 y_true, y_score, n_bins=n_bins, strategy="uniform"
 )
 return pd.DataFrame({"mean_predicted": predicted, "observed_frequency": observed})


def quantile_loss(y_true: np.ndarray, y_pred: np.ndarray, quantile: float) -> float:
 """Pinball loss for a single quantile forecast of cascade severity.

 Used for the severity quantile-loss metric (PLAN §7): each severity
 forecast is scored against the realized severity at the target quantile.
 """
 if not 0.0 < quantile < 1.0:
 raise ValueError(f"quantile must be in (0, 1), got {quantile}")
 y_true = np.asarray(y_true, dtype=float)
 y_pred = np.asarray(y_pred, dtype=float)
 error = y_true - y_pred
 return float(np.mean(np.maximum(quantile * error, (quantile - 1) * error)))
