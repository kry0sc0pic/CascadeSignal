"""Hawkes self-exciting point-process models (CAS-18/CAS-20).

The branching ratio n(t) is the real-time cascade-criticality alarm signal:
each liquidation raises the intensity of the next, and n(t) -> 1 marks the
onset of a self-sustaining cascade.
"""

from cascadesignal.models.harness import baseline_report, walk_forward_oof_scores
from cascadesignal.models.hawkes import (
    HawkesMultivariateBranchingRatio,
    HawkesUnivariateBranchingRatio,
)
from cascadesignal.models.labels import (
    build_liquidation_bars,
    make_bar_labels,
    make_bar_severity_targets,
)

__all__ = [
    "HawkesUnivariateBranchingRatio",
    "HawkesMultivariateBranchingRatio",
    "walk_forward_oof_scores",
    "baseline_report",
    "build_liquidation_bars",
    "make_bar_labels",
    "make_bar_severity_targets",
]
