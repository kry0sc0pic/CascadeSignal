#!/usr/bin/env python3
"""Fig. 2 -- the Hawkes branching ratio vs. naive rate alarms.

Answers "why not just threshold the liquidation rate?" Every model is scored
through the SAME walk-forward out-of-fold harness (models/harness.py): each bar
is scored only by a model trained on strictly-earlier months, so the reported
AUPRC and lead-time frontier carry no in-sample optimism. Left: AUPRC lift over
prevalence (how much better than a coin-flip at the base rate). Right: the
lead-time frontier -- the best recall achievable at each forecast horizon while
holding precision >= 0.5 -- i.e. how far ahead each detector can see.

Uses bar_blocks=5 (not the live bar_blocks=1) purely to keep the ~60-fold
refit tractable; this figure is about RELATIVE ranking, not the live operating
point (that is Figs 1/3/4).

Run: uv run python scripts/paper/fig2_baselines.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve.parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve.parent))

import matplotlib.pyplot as plt # noqa: E402

from sklearn.metrics import precision_recall_curve # noqa: E402

import _common as C # noqa: E402
from cascadesignal.labels.cascade_labeler import load_liquidations # noqa: E402
from cascadesignal.eval.metrics import auprc # noqa: E402
from cascadesignal.models.baselines import ( # noqa: E402
 EwmaRateBaseline,
 RawCountBaseline,
 RollingCountBaseline,
)
from cascadesignal.models.harness import walk_forward_oof_scores # noqa: E402
from cascadesignal.models.hawkes import HawkesUnivariateBranchingRatio # noqa: E402
from cascadesignal.models.labels import build_liquidation_bars, make_bar_labels # noqa: E402
from cascadesignal.eval.splitter import WalkForwardSplitter # noqa: E402

BAR_BLOCKS = 5
HORIZON = 50 # forecast label window (matches thresholds.json horizon_blocks)

MODELS = [
 ("Hawkes n(t)", lambda: HawkesUnivariateBranchingRatio),
 ("raw count", lambda: RawCountBaseline),
 ("rolling rate (20)", lambda: RollingCountBaseline(20)),
 ("EWMA rate (hl=10)", lambda: EwmaRateBaseline(10.0)),
]


def _downsample_pr(recall: np.ndarray, precision: np.ndarray, n: int = 400) -> tuple:
 """Thin a precision-recall curve to ~n points evenly along recall (the raw
 curve has one point per distinct score -- millions of them)."""
 order = np.argsort(recall)
 recall, precision = recall[order], precision[order]
 if len(recall) <= n:
 return recall, precision
 targets = np.linspace(recall.min, recall.max, n)
 idx = np.searchsorted(recall, targets)
 idx = np.clip(idx, 0, len(recall) - 1)
 return recall[idx], precision[idx]


def run_protocol(protocol: str) -> dict:
 """Score every model out-of-fold, then report (a) OOF AUPRC lift over the
 base rate and (b) the OOF precision-recall curve. Labels use ALL labelled
 D-A episodes (not just the 1-3 primary grid points), so there are enough
 positives for a stable ranking comparison."""
 liq = load_liquidations(protocols=[protocol])
 bars = build_liquidation_bars(liq, bar_blocks=BAR_BLOCKS)
 episodes = C.load_episodes(protocol) # ALL D-A episodes
 y = make_bar_labels(bars, episodes, horizon_blocks=HORIZON, primary_only=False)
 splitter = WalkForwardSplitter(min_train_months=6)

 reports = []
 for name, factory in MODELS:
 oof, mask = walk_forward_oof_scores(factory, bars, y, splitter)
 scores = oof[mask]
 labels = y[mask]
 prev = float(labels.mean)
 ap = auprc(labels, scores) if 0 < labels.sum < labels.size else None
 p, r, _ = precision_recall_curve(labels, scores)
 rd, pd_ = _downsample_pr(r, p)
 reports.append({
 "baseline": name, "auprc": ap, "prevalence": prev,
 "auprc_lift": (ap / prev if ap and prev > 0 else None),
 "n_positive": int(labels.sum),
 "pr_recall": rd.tolist, "pr_precision": pd_.tolist,
 })
 print(f" {protocol} {name:<18} AUPRC={ap:.4f} "
 f"lift={reports[-1]['auprc_lift']:.0f}× pos={int(labels.sum)}")
 return {"protocol": protocol, "reports": reports}


def _plot_auprc(ax, res):
 names = [r["baseline"] for r in res["reports"]]
 lifts = [r.get("auprc_lift") or 0.0 for r in res["reports"]]
 colors = [C.SERIES[0]] + [C.COL["muted"]] * (len(names) - 1)
 bars = ax.bar(range(len(names)), lifts, color=colors, alpha=0.9)
 ax.axhline(1.0, color=C.COL["threshold"], ls=":", lw=0.9)
 ax.text(len(names) - 0.5, 1.0, " base rate (1×)", fontsize=6.5, va="bottom",
 ha="right", color=C.COL["threshold"])
 for b, lv in zip(bars, lifts):
 ax.text(b.get_x + b.get_width / 2, lv, f"{lv:.0f}×", ha="center",
 va="bottom", fontsize=7.5)
 ax.set_xticks(range(len(names)))
 ax.set_xticklabels(names, rotation=25, ha="right", fontsize=7)
 ax.set_ylabel("AUPRC lift over base rate (×)", fontsize=8)
 ax.set_title(f"{C.PROTO_LABEL[res['protocol']]} · out-of-fold AUPRC lift",
 fontsize=9)
 ax.set_ylim(bottom=0)


def _plot_pr(ax, res):
 for i, r in enumerate(res["reports"]):
 col = C.SERIES[0] if i == 0 else C.SERIES[i % len(C.SERIES)]
 lw = 2.2 if i == 0 else 1.1
 ap = r["auprc"]
 lab = f"{r['baseline']} (AP={ap:.3f})" if ap is not None else r["baseline"]
 ax.plot(r["pr_recall"], r["pr_precision"], lw=lw, color=col, label=lab,
 alpha=0.95 if i == 0 else 0.75)
 ax.axhline(res["reports"][0]["prevalence"], color=C.COL["threshold"], ls=":",
 lw=0.9)
 ax.text(0.98, res["reports"][0]["prevalence"], " base rate", fontsize=6.5,
 va="bottom", ha="right", color=C.COL["threshold"])
 ax.set_yscale("log")
 ax.set_xlabel("recall", fontsize=8)
 ax.set_ylabel("precision (log)", fontsize=8)
 ax.set_title(f"{C.PROTO_LABEL[res['protocol']]} · out-of-fold precision–recall",
 fontsize=9)
 ax.set_xlim(0, 1.0)
 ax.legend(fontsize=7, loc="upper right")


def main -> None:
 C.apply_style
 results = [run_protocol(p) for p in C.LIVE_PROTOCOLS]

 n = len(results)
 fig, axes = plt.subplots(n, 2, figsize=(11, 3.4 * n))
 axes = np.atleast_2d(axes)
 for i, res in enumerate(results):
 _plot_auprc(axes[i, 0], res)
 _plot_pr(axes[i, 1], res)
 fig.suptitle(
 "Walk-forward out-of-fold: Hawkes n(t) vs. naive liquidation-rate alarms",
 fontsize=11, fontweight="bold",
 )
 fig.tight_layout(rect=(0, 0, 1, 0.97))
 C.save(fig, "fig2_baselines")

 out = C.FIG_DIR / "fig2_baselines.json"
 out.write_text(json.dumps(results, indent=2, default=float))
 print(f" wrote {out}")


if __name__ == "__main__":
 main
