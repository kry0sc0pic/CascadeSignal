#!/usr/bin/env python3
"""Fig. 3 -- operating characteristics at the deployed point.

Left: the false-alarm/recall trade-off swept over the alert threshold at the
DEPLOYED debounce k and live bar clock (bar_blocks=1, persisted fit), with the
calibrated operating point starred and the <=1/week budget marked. This is the
"why this threshold" panel. Right: the actual early-warning lead the deployed
point delivers on each major cascade -- minutes between the debounced alarm and
(a) the cascade's first liquidation and (b) the block by which half the
cascade's USD has already been liquidated.

Run: set -a && source .env && set +a && uv run python scripts/paper/fig3_operating_characteristics.py
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

# Lead-search / cost window around each primary episode. Tied to the detection
# credit window (C.LEAD_WINDOW) so a reported lead can never come from a fire
# that classify_fires would count as a false alarm (outside the credit window)
# otherwise a sensitive signal "leads" a multi-day crisis by hours off adjacent
# turbulence (e.g. Jun 2022 Terra/3AC/Celsius).
PAD = C.LEAD_WINDOW


def far_recall_curve(protocol: str, bars: pd.DataFrame, threshold, k, horizon):
 scores = bars["n_t"].to_numpy
 blocks = bars["end_block"].to_numpy(dtype=np.int64)
 eps = C.load_episodes(protocol)
 weeks = (blocks[-1] - blocks[0]) / C.blocks_per_week(bars)

 grid = np.unique(np.concatenate([
 np.linspace(0.99, 0.9999, 60),
 np.linspace(0.9999, 0.999999, 40),
 [threshold],
 ]))
 prim_idx = set(np.where(eps["is_primary"].to_numpy)[0].tolist)
 n_prim = len(prim_idx)
 rows = []
 for thr in grid:
 fires = blocks[C.debounced_fire_mask(scores, thr, k)]
 cls = C.classify_fires(fires, eps)
 rows.append({
 "threshold": float(thr),
 "far_per_week": len(cls["false_blocks"]) / weeks,
 "recall": len(cls["detected_idx"]) / len(eps),
 "recall_primary": len(cls["detected_idx"] & prim_idx) / n_prim if n_prim else 0.0,
 "is_operating": bool(np.isclose(thr, threshold)),
 })
 return rows, len(eps), n_prim


def episode_leads(protocol: str, bars: pd.DataFrame, threshold, k) -> list[dict]:
 scores = bars["n_t"].to_numpy
 blocks = bars["end_block"].to_numpy(dtype=np.int64)
 all_fires = blocks[C.debounced_fire_mask(scores, threshold, k)]
 liq = C.load_liquidations(protocols=[protocol])
 prim = C.load_episodes(protocol, primary_only=True).sort_values("severity_usd",
 ascending=False)
 out = []
 for _, e in prim.iterrows:
 sb, eb = int(e["start_block"]), int(e["end_block"])
 # first debounced fire in the run-up window [start-PAD, start+PAD]
 cand = all_fires[(all_fires >= sb - PAD) & (all_fires <= sb + PAD)]
 if not len(cand):
 continue
 fire = int(cand.min)
 wl = liq[(liq["block_number"] >= sb - PAD) & (liq["block_number"] <= eb + PAD)]
 wl = wl.sort_values("block_number")
 cum = np.cumsum(wl["amount_usd"].fillna(0.0).to_numpy)
 lblk = wl["block_number"].to_numpy
 half_block = int(lblk[int(np.searchsorted(cum, 0.5 * cum[-1]))]) if len(cum) and cum[-1] > 0 else sb
 name = C.EPISODE_NAMES.get((protocol, sb), str(e["start_time"])[:10])
 out.append({
 "protocol": protocol, "name": name, "fire_block": fire,
 "lead_start_min": (sb - fire) * C.SECONDS_PER_BLOCK / 60.0,
 "lead_half_min": (half_block - fire) * C.SECONDS_PER_BLOCK / 60.0,
 "severity_usd": float(e["severity_usd"]),
 })
 return out


def _plot_frontier(ax, curves):
 for i, (proto, rows, neps, nprim) in enumerate(curves):
 df = pd.DataFrame(rows)
 col = C.SERIES[i]
 ax.plot(df["far_per_week"], df["recall"], "-", color=col, lw=1.4,
 label=f"{C.PROTO_LABEL[proto]} ({neps} D-A episodes)", alpha=0.9)
 op = df[df["is_operating"]].iloc[0]
 ax.plot(op["far_per_week"], op["recall"], marker="*", ms=16, color=col,
 mec="black", mew=0.5, zorder=6)
 ax.annotate(
 f"{int(round(op['recall_primary'] * nprim))}/{nprim} major cascades\n"
 f"@ {op['far_per_week']:.2f}/wk",
 (op["far_per_week"], op["recall"]), fontsize=7, color=col,
 xytext=(8, -2), textcoords="offset points", va="top")
 ax.axvline(1.0, color=C.COL["muted"], ls=":", lw=0.9)
 ax.text(1.0, 0.02, " 1/week budget", fontsize=7, color=C.COL["muted"], rotation=90,
 va="bottom", ha="left")
 ax.set_xlabel("false alarms per week (log)", fontsize=8.5)
 ax.set_ylabel("recall of all labelled D-A episodes", fontsize=8.5)
 ax.set_xscale("log")
 ax.set_title("Threshold trade-off @ deployed debounce k (★ = operating point)",
 fontsize=9)
 ax.set_ylim(0, 1.02)
 ax.legend(fontsize=7.5, loc="lower right")


def _plot_leads(ax, leads):
 labels = [f"{d['name']}\n({C.PROTO_LABEL[d['protocol']]})" for d in leads]
 x = np.arange(len(leads))
 w = 0.38
 ls = [d["lead_start_min"] for d in leads]
 lh = [d["lead_half_min"] for d in leads]
 ax.bar(x - w / 2, ls, w, color=C.SERIES[0], label="lead before first liquidation")
 ax.bar(x + w / 2, lh, w, color=C.COL["cost"], label="lead before 50% of USD liquidated")
 for xi, (a, b) in enumerate(zip(ls, lh)):
 ax.text(xi - w / 2, a, f"{a:.0f}", ha="center", va="bottom", fontsize=7.5)
 ax.text(xi + w / 2, b, f"{b:.0f}", ha="center", va="bottom", fontsize=7.5)
 ax.axhline(0, color="black", lw=0.6)
 ax.set_xticks(x)
 ax.set_xticklabels(labels, fontsize=7.5)
 ax.set_ylabel("early-warning lead (minutes)", fontsize=8.5)
 ax.set_title("Warning lead on each major cascade (deployed operating point)",
 fontsize=9)
 ax.legend(fontsize=7.5, loc="upper right")


def main -> None:
 C.apply_style
 curves, leads = [], []
 for p in C.LIVE_PROTOCOLS:
 thr, k, h = C.operating_point(p)
 bars = C.scored_bars(p)
 rows, neps, nprim = far_recall_curve(p, bars, thr, k, h)
 curves.append((p, rows, neps, nprim))
 leads.extend(episode_leads(p, bars, thr, k))
 op = next(r for r in rows if r["is_operating"])
 print(f" {p}: operating FAR={op['far_per_week']:.2f}/wk recall_all={op['recall']:.2f} "
 f"recall_primary={op['recall_primary']:.2f}")
 for d in leads:
 print(f" {d['protocol']} {d['name']}: lead_start={d['lead_start_min']:.0f}min "
 f"lead_half={d['lead_half_min']:.0f}min")

 fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))
 _plot_frontier(axes[0], curves)
 _plot_leads(axes[1], leads)
 fig.suptitle("Operating characteristics: principled threshold + real early warning",
 fontsize=11, fontweight="bold")
 fig.tight_layout(rect=(0, 0, 1, 0.95))
 C.save(fig, "fig3_operating_characteristics")
 (C.FIG_DIR / "fig3_operating_characteristics.json").write_text(
 json.dumps({"curves": [{"protocol": p, "rows": r} for p, r, _, _ in curves],
 "leads": leads}, indent=2))


if __name__ == "__main__":
 main
