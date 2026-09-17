"""Economic USD-protectable metric, the headline figure.

"Protectable USD" accounting ( -- this is the precise
definition, pinned here rather than left implicit): an episode's
`severity_usd` ( label field;
ADR-001) counts as protectable iff the alert stream produced a true
detection of that episode at the given lead time (`alerts.classify_alerts`).
The headline number is the sum of `severity_usd` over protected episodes
"how much liquidation volume the system would have had a chance to act on,
had it converted every timely alert into an intervention." This is an upper
bound (it assumes a perfect, instant intervention on every timely alert), not
a claim that this USD would actually have been saved.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from cascadesignal.eval.alerts import classify_alerts, raise_alert_events


@dataclass(frozen=True)
class UsdProtectableResult:
 point_estimate: float
 ci_low: float
 ci_high: float
 n_episodes: int
 n_protected: int


def usd_protectable(
 scores: np.ndarray,
 blocks: np.ndarray,
 episodes: pd.DataFrame,
 threshold: float,
 lead_blocks: int,
 n_bootstrap: int = 2000,
 ci_level: float = 0.95,
 seed: int = 42,
) -> UsdProtectableResult:
 """Total `severity_usd` over episodes detected with >= lead_blocks of
 warning, with a percentile bootstrap CI over episodes.

 Episodes (not raw time blocks) are the resampling unit: the metric is a
 sum over a moderate number of roughly-independent discrete events, not a
 statistic over an autocorrelated time series (that's block
 bootstrap, for continuous per-block metrics like AUPRC).
 """
 if len(episodes) == 0:
 raise ValueError("episodes must be non-empty")

 alert_blocks = raise_alert_events(scores, blocks, threshold)
 _, detected = classify_alerts(alert_blocks, episodes, lead_blocks)
 severities = episodes["severity_usd"].to_numpy(dtype=float)
 protected_mask = np.zeros(len(episodes), dtype=bool)
 protected_mask[list(detected)] = True

 point_estimate = float(severities[protected_mask].sum)

 rng = np.random.default_rng(seed)
 n = len(episodes)
 boot_totals = np.empty(n_bootstrap)
 for b in range(n_bootstrap):
 sample_idx = rng.integers(0, n, size=n)
 boot_totals[b] = severities[sample_idx][protected_mask[sample_idx]].sum

 alpha = (1 - ci_level) / 2
 ci_low, ci_high = np.quantile(boot_totals, [alpha, 1 - alpha])

 return UsdProtectableResult(
 point_estimate=point_estimate,
 ci_low=float(ci_low),
 ci_high=float(ci_high),
 n_episodes=n,
 n_protected=int(protected_mask.sum),
 )
