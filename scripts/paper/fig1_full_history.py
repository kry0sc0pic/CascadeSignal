#!/usr/bin/env python3
"""Fig. 1 -- one full-history run, both regimes.

Top row: each major D-A cascade cropped to its own window -- n(t) climbing
through the alert threshold, the debounced alarm fire, and the liquidation cost
accumulating underneath. Bottom row: the SAME operating point over each
protocol's entire history, with every false alarm marked -- showing how seldom
n(t) crosses when there is no cascade. Cropped cascades (it fires early) and
quiet periods (it rarely fires) in one figure.

Run: set -a && source .env && set +a && uv run python scripts/paper/fig1_full_history.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402

import _common as C  # noqa: E402

PAD_BLOCKS = 3000  # ~10 h of context on each side of the episode span


def _crop_panel(ax, protocol, bars, start_block, end_block, name, threshold, k, horizon):
    lo, hi = start_block - PAD_BLOCKS, end_block + PAD_BLOCKS
    w = bars[(bars["end_block"] >= lo) & (bars["end_block"] <= hi)]
    blk = w["end_block"].to_numpy()
    nt = w["n_t"].to_numpy()
    mins = (blk - start_block) * C.SECONDS_PER_BLOCK / 60.0

    ax.axvspan(0, (end_block - start_block) * C.SECONDS_PER_BLOCK / 60.0,
               color=C.COL["episode"], alpha=0.12, lw=0, label="cascade span")
    ax.plot(mins, nt, color=C.COL["nt"], lw=1.1, label="n(t)")
    if threshold is not None:
        ax.axhline(threshold, color=C.COL["threshold"], ls="--", lw=0.9,
                   label="threshold")

    # debounced alarm within the crop
    fires = C.debounced_alarm_blocks(nt, blk, threshold, k)
    fires = fires[(fires >= lo) & (fires <= hi)]
    lead_txt = ""
    if len(fires):
        fmin = (fires[0] - start_block) * C.SECONDS_PER_BLOCK / 60.0
        ax.axvline(fmin, color=C.COL["alarm"], lw=1.3)
        ax.plot([fmin], [threshold], marker="v", ms=7, color=C.COL["alarm"],
                zorder=5, label="alarm")

    # cumulative liquidation cost within the window (right axis)
    liq = C.load_liquidations(protocols=[protocol])
    wl = liq[(liq["block_number"] >= lo) & (liq["block_number"] <= hi)].sort_values(
        "block_number"
    )
    ax2 = ax.twinx()
    if len(wl):
        lblk = wl["block_number"].to_numpy()
        usd = wl["amount_usd"].fillna(0.0).to_numpy()
        cum = np.cumsum(usd)
        total = cum[-1] if cum[-1] > 0 else 1.0
        lmin = (lblk - start_block) * C.SECONDS_PER_BLOCK / 60.0
        ax2.fill_between(lmin, 0, cum / 1e6, step="post", color=C.COL["cost_fill"],
                         alpha=0.55, lw=0)
        ax2.plot(lmin, cum / 1e6, drawstyle="steps-post", color=C.COL["cost"], lw=0.9)
        # lead to 50% of window cost
        half_idx = int(np.searchsorted(cum, 0.5 * total))
        if len(fires) and half_idx < len(lblk):
            lead_blk = int(lblk[half_idx]) - int(fires[0])
            lead_txt = f"+{lead_blk * C.SECONDS_PER_BLOCK / 60.0:.0f} min lead"
    ax2.set_ylabel("cum $M", color=C.COL["cost"], fontsize=7)
    ax2.tick_params(axis="y", labelsize=6, colors=C.COL["cost"])
    ax2.set_ylim(bottom=0)

    ax.set_title(f"{C.PROTO_LABEL[protocol]} · {name}", fontsize=8.5)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("minutes from cascade start", fontsize=7)
    ax.set_ylabel("n(t)", color=C.COL["nt"], fontsize=7)
    ax.tick_params(axis="y", labelcolor=C.COL["nt"])
    ax.margins(x=0)
    if lead_txt:
        ax.text(0.03, 0.06, lead_txt, transform=ax.transAxes, fontsize=8,
                color=C.COL["alarm"], fontweight="bold",
                bbox=dict(facecolor="white", edgecolor="none", alpha=0.85, pad=1.5))


def _strip_panel(ax, protocol, bars, threshold, k, horizon):
    """Full-history alarm frequency: monthly false-alarm count (sparse red bars
    with long gaps == quiet), the ~1/week budget line, and a green marker at
    each detected primary cascade. Raw per-block n(t) is far too spiky to plot
    over 5 years -- its envelope saturates -- so the panel shows how OFTEN the
    operating point fires, which is the quiet-period story."""
    blk = bars["end_block"].to_numpy()
    nt = bars["n_t"].to_numpy()
    t = pd.to_datetime(bars["end_time"], utc=True).reset_index(drop=True)

    def to_time(bs: np.ndarray) -> pd.Series:
        pos = np.clip(np.searchsorted(blk, bs), 0, len(t) - 1)
        return t.iloc[pos].reset_index(drop=True)

    fires = C.debounced_alarm_blocks(nt, blk, threshold, k)
    eps = C.load_episodes(protocol)
    cls = C.classify_fires(fires, eps)
    fb = cls["false_blocks"]

    ymax = 1
    if len(fb):
        ft = to_time(fb).dt.tz_localize(None)
        months = ft.dt.to_period("M").value_counts().sort_index()
        mx = months.index.to_timestamp()
        ax.bar(mx, months.to_numpy(), width=22, color=C.COL["false"], alpha=0.85,
               align="center", label="false alarms / month")
        ymax = max(ymax, int(months.max()))

    # ~1 alarm/week budget, as a monthly-equivalent reference line
    ax.axhline(52 / 12.0, color=C.COL["muted"], ls=":", lw=0.9)
    ax.text(t.iloc[2], 52 / 12.0 + 0.15, "1 / week budget", fontsize=6.5,
            color=C.COL["muted"], va="bottom")

    # detected primary cascades
    prim = eps[eps["is_primary"]]
    pt = to_time(prim["start_block"].to_numpy())
    ax.plot(pt, np.full(len(pt), ymax * 1.12), marker="v", ms=8, ls="none",
            color=C.COL["alarm"], zorder=5, label="cascade detected")

    weeks = len(bars) / C.blocks_per_week(bars)
    far = len(fb) / weeks if weeks else 0.0
    ax.set_title(
        f"{C.PROTO_LABEL[protocol]} · full history "
        f"({len(bars) / 1e6:.1f} M blocks) — {len(fb)} false alarms "
        f"= {far:.2f}/week, k={k}",
        fontsize=8.5,
    )
    ax.set_ylim(0, ymax * 1.28)
    ax.set_ylabel("false alarms / month", fontsize=7)
    ax.margins(x=0.01)


def main() -> None:
    C.apply_style()
    bars = {p: C.scored_bars(p) for p in C.LIVE_PROTOCOLS}
    ops = {p: C.operating_point(p) for p in C.LIVE_PROTOCOLS}

    # crops: all primaries, most-severe first, capped to a clean top row of 4
    crops = []
    for p in C.LIVE_PROTOCOLS:
        prim = C.load_episodes(p, primary_only=True).sort_values(
            "severity_usd", ascending=False
        )
        for _, e in prim.iterrows():
            sb = int(e["start_block"])
            name = C.EPISODE_NAMES.get((p, sb), str(e["start_time"])[:10])
            crops.append((p, sb, int(e["end_block"]), name))
    crops = crops[:4]

    fig = plt.figure(figsize=(13, 6.6))
    gs = GridSpec(2, 4, figure=fig, height_ratios=[1.0, 0.85], hspace=0.55, wspace=0.5)

    for i, (p, sb, eb, name) in enumerate(crops):
        thr, k, h = ops[p]
        _crop_panel(fig.add_subplot(gs[0, i]), p, bars[p], sb, eb, name, thr, k, h)

    for j, p in enumerate(C.LIVE_PROTOCOLS):
        thr, k, h = ops[p]
        _strip_panel(fig.add_subplot(gs[1, j * 2:(j + 1) * 2]), p, bars[p], thr, k, h)

    fig.suptitle(
        "Hawkes branching-ratio alarm: early on cascades (top), quiet otherwise (bottom)",
        fontsize=11, fontweight="bold", y=0.99,
    )
    handles = [
        plt.Line2D([], [], color=C.COL["nt"], lw=1.2, label="n(t) branching ratio"),
        plt.Line2D([], [], color=C.COL["threshold"], ls="--", lw=1, label="alert threshold"),
        plt.Line2D([], [], color=C.COL["alarm"], marker="v", ls="none",
                   label="alarm fire / cascade detected"),
        plt.Line2D([], [], color=C.COL["cost"], lw=1.2, label="cumulative liquidation $"),
        plt.matplotlib.patches.Patch(color=C.COL["false"], alpha=0.85,
                                     label="false alarms / month"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=5, fontsize=8,
               bbox_to_anchor=(0.5, -0.02))
    C.save(fig, "fig1_full_history")


if __name__ == "__main__":
    main()
