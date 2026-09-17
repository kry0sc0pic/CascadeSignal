"""Bar-by-bar-safe scoring on top of `HawkesUnivariateBranchingRatio`.

Real gap in the algorithm core, found while wiring this up (not touching
`models/hawkes.py` to fix it -- documenting the workaround here instead, per
CLAUDE.md's "wrap, don't rewrite" + "explain before touching core"):
`HawkesUnivariateBranchingRatio.score(X)` reads `self._r_end` as the carried
starting state but never writes it back. Every existing caller
(`models/harness.py:walk_forward_oof_scores`) calls `score` exactly ONCE
per fold, on the whole fold's bars in one shot -- so `_r_end` never needs to
move between calls. A live daemon violates that assumption: it calls
`score` repeatedly, once per newly-closed batch of bars, and each call
needs to continue from where the *previous live call* left off, not from
whatever `_r_end` fit last set. `score_and_advance` does exactly what
`fit` does at the end of its own recursion (same formula, applied to a
scoring batch instead of a fit batch) and writes the result back onto the
model, so the next call picks up correctly.

`score_and_advance` also returns the per-bar `r` (self-excited history) and
`lam` (conditional intensity) it already computes en route to `n_t`
intermediate state the algorithm itself produces, exposed here for the live
UI's "internal state" panel. `lam = mu + alpha*r` matches
`_excitation_and_lambda`'s no-covariate branch exactly; the live path never
sets `cov_col`, so that branch is always the one in play. These are read
from the pre-advance `r` array (computed before `model._r_end` is mutated
below), not a second call into the model, so no `hawkes.py` change is
needed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from cascadesignal.models.hawkes import HawkesUnivariateBranchingRatio, _decayed_history


@dataclass
class ScoringResult:
 n_t: np.ndarray
 r: np.ndarray
 lam: np.ndarray


def score_and_advance(
 model: HawkesUnivariateBranchingRatio, bars: pd.DataFrame
) -> ScoringResult:
 """Score `bars` (already fit model) and advance `model._r_end` past this
 batch, in place, so the next call continues the recursion correctly.
 Returns n(t), r, and lambda, each aligned to `bars`' row order."""
 if len(bars) == 0:
 empty = np.empty(0, dtype=float)
 return ScoringResult(n_t=empty, r=empty, lam=empty)
 scores = model.score(bars)
 mu, alpha, beta = model.params
 decay = np.exp(-beta)
 marks = bars[model.mark_col].to_numpy(dtype=float)
 r = _decayed_history(marks, decay, r0=model._r_end) # noqa: SLF001
 lam = mu + alpha * r
 model._r_end = float(decay * (r[-1] + marks[-1])) # noqa: SLF001
 return ScoringResult(n_t=scores, r=r, lam=lam)


__all__ = ["score_and_advance", "ScoringResult"]
