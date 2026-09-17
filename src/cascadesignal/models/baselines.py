"""Naive alarm baselines for the Hawkes comparison (paper Fig. 2).

The Hawkes branching ratio n(t) has to earn its place against the obvious
"just threshold the liquidation rate" alternatives a reviewer will ask about.
Each baseline here implements the same fit(X, y)->self / score(X)->np.ndarray
contract as `HawkesUnivariateBranchingRatio`, so it drops straight into
`models/harness.py`'s walk-forward OOF harness and is scored on exactly the
same folds, labels, and metrics.

All three score off the `n_liquidations` mark only (the same input the Hawkes
alarm sees) and are causal: the score for bar i uses counts strictly before
bar i, matching the Hawkes convention that R_i excludes the bar's own count.
The one exception, `RawCountBaseline`, deliberately uses the current bar's
count -- it is the degenerate "alarm when this block has many liquidations"
floor, which by construction cannot lead the cascade.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_MARK = "n_liquidations"


class RawCountBaseline:
 """Score = the current bar's liquidation count. The dumbest possible
 detector; has no lead by construction (it only knows the present bar)."""

 name = "raw_count"

 def fit(self, X: pd.DataFrame, y: np.ndarray | None = None) -> "RawCountBaseline":
 return self

 def score(self, X: pd.DataFrame) -> np.ndarray:
 return X[_MARK].to_numpy(dtype=float)


class RollingCountBaseline:
 """Score = trailing sum of liquidation counts over the previous
 `window_bars` bars (strictly before the current bar). A plain moving-average
 rate alarm -- the "why not just a moving average?" baseline."""

 def __init__(self, window_bars: int = 20):
 self.window_bars = window_bars
 self.name = f"rolling_count_{window_bars}"

 def fit(self, X: pd.DataFrame, y: np.ndarray | None = None) -> "RollingCountBaseline":
 return self

 def score(self, X: pd.DataFrame) -> np.ndarray:
 m = X[_MARK].to_numpy(dtype=float)
 cum = np.concatenate([[0.0], np.cumsum(m)]) # cum[i] = sum(m[:i])
 idx = np.arange(len(m))
 lo = np.clip(idx - self.window_bars, 0, None)
 return cum[idx] - cum[lo] # sum(m[lo:idx]) -- excludes bar idx


class EwmaRateBaseline:
 """Score = exponentially-weighted moving average of the liquidation count,
 over counts strictly before the current bar. A smoother trailing-rate
 alarm than the boxcar `RollingCountBaseline`."""

 def __init__(self, halflife_bars: float = 10.0):
 self.halflife_bars = halflife_bars
 self.name = f"ewma_rate_{int(halflife_bars)}"

 def fit(self, X: pd.DataFrame, y: np.ndarray | None = None) -> "EwmaRateBaseline":
 return self

 def score(self, X: pd.DataFrame) -> np.ndarray:
 m = pd.Series(X[_MARK].to_numpy(dtype=float)).shift(1).fillna(0.0)
 return m.ewm(halflife=self.halflife_bars, adjust=False).mean.to_numpy


__all__ = ["RawCountBaseline", "RollingCountBaseline", "EwmaRateBaseline"]
