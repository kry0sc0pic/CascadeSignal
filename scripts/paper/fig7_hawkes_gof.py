#!/usr/bin/env python3
"""Fig. 7 -- is the Hawkes model actually well-specified?

The alarm is only legitimate if the fitted process explains the liquidation
counts. Two standard checks per protocol:

* Randomized quantile (PIT) residuals (Dunn-Smyth): for each bar with fitted
 intensity lambda_i and observed count m_i, u_i = F(m_i-1;lambda_i) +
 U*f(m_i;lambda_i). If the conditional-Poisson/Hawkes model is correct the u_i
 are Uniform(0,1), so their histogram is flat. Reported with a Kolmogorov-
 Smirnov distance from uniform.
* Stationarity: the exponential-kernel branching ratio
 alpha*e^-beta/(1-e^-beta) must be < 1 for a stable (non-explosive) process.

Run: set -a && source .env && set +a && uv run python scripts/paper/fig7_hawkes_gof.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve.parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve.parent))

import matplotlib.pyplot as plt # noqa: E402
from scipy import stats # noqa: E402

import _common as C # noqa: E402
from cascadesignal.labels.cascade_labeler import load_liquidations # noqa: E402
from cascadesignal.live.state import load_state # noqa: E402
from cascadesignal.models.hawkes import HawkesUnivariateBranchingRatio # noqa: E402
from cascadesignal.models.labels import build_liquidation_bars # noqa: E402


def gof(protocol: str, seed: int = 7) -> dict:
 state = load_state(protocol)
 liq = load_liquidations(protocols=[protocol])
 bars = build_liquidation_bars(liq, bar_blocks=state.bar_blocks)
 model = HawkesUnivariateBranchingRatio
 model._params = (state.mu, state.alpha, state.beta) # noqa: SLF001
 model._r_end = 0.0 # noqa: SLF001

 m = bars["n_liquidations"].to_numpy(dtype=float)
 lam = np.clip(model.intensity(bars), 1e-12, None)

 # (1) randomized PIT residual KS distance from uniform
 rng = np.random.default_rng(seed)
 a = stats.poisson.cdf(m - 1, lam)
 b = stats.poisson.cdf(m, lam)
 u = a + rng.uniform(size=len(m)) * (b - a)
 ks = stats.kstest(u, "uniform")

 # (2) intensity calibration: bin bars by predicted intensity lambda on a log
 # scale (value bins, not quantile bins -- lambda is pinned at mu for the vast
 # majority of sparse/quiet bars, so quantile bins would collapse onto that
 # one value). Compare mean predicted vs mean observed count per bin. A
 # well-specified conditional intensity lands on y = x across the range
 # i.e. bars it calls high-intensity really do carry proportionally more
 # events. Only bins with >= 50 bars are kept (stable means).
 edges = np.logspace(np.log10(lam.min), np.log10(lam.max), 22)
 which = np.clip(np.digitize(lam, edges) - 1, 0, len(edges) - 2)
 pred, obs = [], []
 for j in range(len(edges) - 1):
 sel = which == j
 if sel.sum >= 50:
 pred.append(float(lam[sel].mean))
 obs.append(float(m[sel].mean))
 pred = np.array(pred)
 obs = np.array(obs)

 decay = np.exp(-state.beta)
 branching = state.alpha * decay / (1 - decay)
 return {
 "protocol": protocol, "n_bars": int(len(m)),
 "ks_stat": float(ks.statistic), "ks_p": float(ks.pvalue),
 "branching_ratio": float(branching),
 "total_pred": float(lam.sum), "total_obs": float(m.sum),
 "mu": state.mu, "alpha": state.alpha, "beta": state.beta,
 "cal_pred": pred.tolist, "cal_obs": obs.tolist,
 }


def _plot(ax, res):
 pred = np.array(res["cal_pred"])
 obs = np.array(res["cal_obs"])
 lim_lo = max(min(pred.min, obs[obs > 0].min) * 0.6, 1e-6)
 lim_hi = max(pred.max, obs.max) * 1.6
 ax.plot([lim_lo, lim_hi], [lim_lo, lim_hi], ls="--", color=C.COL["threshold"],
 lw=1.0, label="perfect calibration (y=x)")
 ax.scatter(pred, obs, s=16, color=C.SERIES[0], alpha=0.85, zorder=5,
 label="intensity bin (≥50 bars)")
 ax.set_xscale("log")
 ax.set_yscale("log")
 ax.set_xlim(lim_lo, lim_hi)
 ax.set_ylim(lim_lo, lim_hi)
 ax.set_xlabel("mean predicted intensity λ (per bin)", fontsize=8.5)
 ax.set_ylabel("mean observed liquidations (per bin)", fontsize=8.5)
 stable = "stationary ✓" if res["branching_ratio"] < 1 else "NON-STATIONARY"
 ax.set_title(
 f"{C.PROTO_LABEL[res['protocol']]} · branching n={res['branching_ratio']:.2f} "
 f"({stable}) · PIT KS D={res['ks_stat']:.4f}",
 fontsize=9)
 ax.legend(fontsize=7.5, loc="upper left")


def main -> None:
 C.apply_style
 results = [gof(p) for p in C.LIVE_PROTOCOLS]
 for r in results:
 print(f" {r['protocol']}: KS D={r['ks_stat']:.4f} (p={r['ks_p']:.2e}) "
 f"branching={r['branching_ratio']:.3f} n_bars={r['n_bars']}")

 fig, axes = plt.subplots(1, len(results), figsize=(12, 4.2))
 axes = np.atleast_1d(axes)
 for ax, res in zip(axes, results):
 _plot(ax, res)
 fig.suptitle("Hawkes goodness-of-fit: intensity calibration + stationarity",
 fontsize=11, fontweight="bold")
 fig.tight_layout(rect=(0, 0, 1, 0.95))
 C.save(fig, "fig7_hawkes_gof")
 (C.FIG_DIR / "fig7_hawkes_gof.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
 main
