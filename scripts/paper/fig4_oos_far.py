#!/usr/bin/env python3
"""Fig. 4 -- out-of-sample false-alarm rate, by year.

The threshold is calibrated against a handful of episodes, all in 2021-2022
(v2) / 2025 (v3). Every OTHER year is a cascade-free stretch the operating
point never saw -- a clean out-of-sample false-alarm test. Bars show the
debounced operating point's false alarms per week each year, split into
in-sample years (a labelled cascade present) and out-of-sample years, against
the <=1/week budget. If OOS bars are not systematically worse than in-sample,
the operating point is not overfit.

Run: set -a && source .env && set +a && uv run python scripts/paper/fig4_oos_far.py
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


def per_year(protocol: str) -> dict:
 thr, k, _ = C.operating_point(protocol)
 bars = C.scored_bars(protocol)
 blocks = bars["end_block"].to_numpy(dtype=np.int64)
 year = pd.to_datetime(bars["end_time"], utc=True).dt.year.to_numpy
 bpw = C.blocks_per_week(bars)

 eps = C.load_episodes(protocol)
 fires = C.debounced_alarm_blocks(bars["n_t"].to_numpy, blocks, thr, k)
 false_b = C.classify_fires(fires, eps)["false_blocks"]
 false_year = year[np.clip(np.searchsorted(blocks, false_b), 0, len(year) - 1)]
 ep_years = set(pd.to_datetime(eps["start_time"], utc=True).dt.year.tolist)

 rows = []
 for y in sorted(set(year.tolist)):
 n_bars = int((year == y).sum)
 weeks = n_bars / bpw # bar_blocks == 1
 n_false = int((false_year == y).sum)
 rows.append({
 "year": int(y), "weeks": round(weeks, 1), "false_alarms": n_false,
 "far_per_week": n_false / weeks if weeks else 0.0,
 "in_sample": y in ep_years,
 })
 oos = [r for r in rows if not r["in_sample"] and r["weeks"] > 1]
 pooled = (sum(r["false_alarms"] for r in oos) / sum(r["weeks"] for r in oos)
 if oos else None)
 return {"protocol": protocol, "k": k, "rows": rows, "pooled_oos_far": pooled}


def _plot(ax, res):
 rows = res["rows"]
 yrs = [r["year"] for r in rows]
 fars = [r["far_per_week"] for r in rows]
 colors = [C.COL["muted"] if r["in_sample"] else C.SERIES[0] for r in rows]
 bars = ax.bar(yrs, fars, color=colors, alpha=0.9)
 for b, r in zip(bars, rows):
 ax.text(b.get_x + b.get_width / 2, r["far_per_week"],
 f"{r['false_alarms']}", ha="center", va="bottom", fontsize=7)
 ax.axhline(1.0, color=C.COL["threshold"], ls=":", lw=1.0)
 ax.text(yrs[0], 1.02, "1/week budget", fontsize=7, color=C.COL["threshold"],
 va="bottom")
 pooled = res["pooled_oos_far"]
 sub = f"pooled OOS FAR = {pooled:.2f}/week" if pooled is not None else ""
 ax.set_title(f"{C.PROTO_LABEL[res['protocol']]} (k={res['k']}) — {sub}", fontsize=9.5)
 ax.set_ylabel("false alarms / week", fontsize=8.5)
 ax.set_xticks(yrs)


def main -> None:
 C.apply_style
 results = [per_year(p) for p in C.LIVE_PROTOCOLS]
 for r in results:
 print(f" {r['protocol']}: pooled OOS FAR = {r['pooled_oos_far']}")

 fig, axes = plt.subplots(1, len(results), figsize=(12, 4.2))
 axes = np.atleast_1d(axes)
 for ax, res in zip(axes, results):
 _plot(ax, res)
 handles = [
 plt.matplotlib.patches.Patch(color=C.COL["muted"], label="in-sample year (cascade labelled)"),
 plt.matplotlib.patches.Patch(color=C.SERIES[0], label="out-of-sample year"),
 ]
 fig.legend(handles=handles, loc="lower center", ncol=2, fontsize=8.5,
 bbox_to_anchor=(0.5, -0.03))
 fig.suptitle("Out-of-sample false-alarm rate holds year to year",
 fontsize=11, fontweight="bold")
 fig.tight_layout(rect=(0, 0.03, 1, 0.95))
 C.save(fig, "fig4_oos_far")
 (C.FIG_DIR / "fig4_oos_far.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
 main
