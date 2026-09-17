#!/usr/bin/env python3
"""Fig. 6 -- the persistence filter (debounce k) trade-off.

The alarm fires only after k of the last W bars cross the threshold (ADR-008
tolerant debounce; W = DEBOUNCE_WINDOW). Raising k
suppresses isolated single-bar n(t) spikes (most false alarms) but delays the
fire by up to k-1 bars, costing lead. This sweeps k at the deployed threshold
and plots both sides: false alarms per week (down, good) and mean early-warning
lead on the major cascades (down, bad), so the chosen operating k is visibly a
knee, not a free lunch. The deployed k is marked.

Run: set -a && source .env && set +a && uv run python scripts/paper/fig6_debounce_ablation.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve.parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve.parent))

import matplotlib.pyplot as plt # noqa: E402

import _common as C # noqa: E402

K_VALUES = [1, 2, 3, 5, 7, 10, 15, 20, 30, 50]
PAD = 3000


def sweep(protocol: str) -> dict:
 thr, k_dep, _ = C.operating_point(protocol)
 bars = C.scored_bars(protocol)
 scores = bars["n_t"].to_numpy
 blocks = bars["end_block"].to_numpy(dtype=np.int64)
 weeks = (blocks[-1] - blocks[0]) / C.blocks_per_week(bars)
 eps = C.load_episodes(protocol)
 prim = C.load_episodes(protocol, primary_only=True)
 starts = prim["start_block"].to_numpy(dtype=np.int64)
 n_prim = len(starts)

 rows = []
 for k in K_VALUES:
 fires = blocks[C.debounced_fire_mask(scores, thr, k)]
 far = len(C.classify_fires(fires, eps)["false_blocks"]) / weeks
 leads = []
 for s in starts:
 cand = fires[(fires >= s - PAD) & (fires < s + PAD)]
 if len(cand):
 leads.append((s - int(cand.min)) * C.SECONDS_PER_BLOCK / 60.0)
 rows.append({
 "k": k, "far_per_week": far,
 "mean_lead_min": float(np.mean(leads)) if leads else 0.0,
 "n_detected_primary": len(leads),
 })
 return {"protocol": protocol, "k_deployed": k_dep, "n_primary": n_prim, "rows": rows}


def _plot(ax, res):
 df = pd.DataFrame(res["rows"])
 ax.plot(df["k"], df["far_per_week"], marker="o", ms=4, color=C.COL["false"],
 lw=1.6, label="false alarms/week")
 ax.axhline(1.0, color=C.COL["muted"], ls=":", lw=0.9)
 ax.set_xlabel(f"debounce k (within window W={C.DEBOUNCE_WINDOW})", fontsize=8.5)
 ax.set_ylabel("false alarms / week", color=C.COL["false"], fontsize=8.5)
 ax.tick_params(axis="y", labelcolor=C.COL["false"])
 ax.set_ylim(bottom=0)

 ax2 = ax.twinx
 ax2.plot(df["k"], df["mean_lead_min"], marker="s", ms=4, color=C.COL["alarm"],
 lw=1.6, label="mean lead")
 ax2.set_ylabel("mean lead on major cascades (min)", color=C.COL["alarm"],
 fontsize=8.5)
 ax2.tick_params(axis="y", labelcolor=C.COL["alarm"])
 ax2.axhline(0, color=C.COL["alarm"], ls=":", lw=0.7)
 ax2.set_ylim(bottom=min(0, df["mean_lead_min"].min) * 1.15)

 kd = res["k_deployed"]
 ax.axvline(kd, color="black", ls="--", lw=1.0)
 ax.text(kd, ax.get_ylim[1] * 0.96, f" deployed k={kd}", fontsize=8,
 va="top", ha="left")

 # note if any primary drops out at high k
 lost = df[df["n_detected_primary"] < res["n_primary"]]
 note = ""
 if len(lost):
 n = res["n_primary"]
 noun = "the major cascade" if n == 1 else f"all {n} major cascades"
 kmax = int(df[df["n_detected_primary"] == n]["k"].max)
 note = f" ({noun} still caught through k={kmax})"
 ax.set_title(f"{C.PROTO_LABEL[res['protocol']]}{note}", fontsize=9.5)


def main -> None:
 C.apply_style
 results = [sweep(p) for p in C.LIVE_PROTOCOLS]
 for res in results:
 print(f" {res['protocol']} (deployed k={res['k_deployed']}):")
 for r in res["rows"]:
 print(f" k={r['k']:>2} FAR={r['far_per_week']:.2f}/wk "
 f"lead={r['mean_lead_min']:.0f}min "
 f"primary={r['n_detected_primary']}/{res['n_primary']}")

 fig, axes = plt.subplots(1, len(results), figsize=(12.5, 4.4))
 axes = np.atleast_1d(axes)
 for ax, res in zip(axes, results):
 _plot(ax, res)
 handles = [
 plt.Line2D([], [], color=C.COL["false"], marker="o", label="false alarms/week"),
 plt.Line2D([], [], color=C.COL["alarm"], marker="s", label="mean lead on major cascades"),
 plt.Line2D([], [], color="black", ls="--", label="deployed k"),
 ]
 fig.legend(handles=handles, loc="lower center", ncol=3, fontsize=8.5,
 bbox_to_anchor=(0.5, -0.04))
 fig.suptitle("Debounce filter: fewer false alarms vs. lead cost", fontsize=11,
 fontweight="bold")
 fig.tight_layout(rect=(0, 0.03, 1, 0.95))
 C.save(fig, "fig6_debounce_ablation")
 (C.FIG_DIR / "fig6_debounce_ablation.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
 main
