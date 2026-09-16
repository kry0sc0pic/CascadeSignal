#!/usr/bin/env python3
"""Fig. 8 -- the branching-ratio signal is not Aave-specific.

The same univariate Hawkes n(t) built on each protocol's own liquidation-count
stream rises into that protocol's largest cascade -- across two lending designs
the alarm was tuned on (Aave v2/v3, persisted live fit) and two it was never
tuned on and that price no USD (Compound v2's cToken model, Maker's CDP/urn
model, fresh full-sample fits). n(t) climbing into every panel is evidence the
signal is a general property of liquidation clustering, not an Aave artifact.

Compound/Maker panels use a full-sample MLE fit (no persisted live operating
point exists for them) and a 99.9th-percentile reference level in place of a
calibrated threshold -- illustrative of the signal, not a tuned detector.

Run: uv run python scripts/paper/fig8_cross_protocol.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import matplotlib.pyplot as plt  # noqa: E402

import _common as C  # noqa: E402

PAD = 4000


def panel_data(protocol: str) -> dict:
    if protocol in C.LIVE_PROTOCOLS:
        bars = C.scored_bars(protocol)
        thr, _, _ = C.operating_point(protocol)
        fitted = "persisted live fit"
    else:
        bars, _ = C.fresh_scored_bars(protocol, bar_blocks=1)
        thr = float(np.quantile(bars["n_t"].to_numpy(), 0.999))
        fitted = "full-sample fit · 99.9th-pct level"

    prim = C.load_episodes(protocol, primary_only=True)
    key = "severity_usd" if prim["severity_usd"].max() > 0 else "num_positions"
    e = prim.sort_values(key, ascending=False).iloc[0]
    sb, eb = int(e["start_block"]), int(e["end_block"])
    w = bars[(bars["end_block"] >= sb - PAD) & (bars["end_block"] <= eb + PAD)]
    name = C.EPISODE_NAMES.get((protocol, sb), str(e["start_time"])[:10])
    return {
        "protocol": protocol, "fitted": fitted, "threshold": thr,
        "name": name, "start_block": sb, "end_block": eb,
        "mins": ((w["end_block"].to_numpy() - sb) * C.SECONDS_PER_BLOCK / 60.0),
        "nt": w["n_t"].to_numpy(),
    }


def _plot(ax, d):
    span_end = (d["end_block"] - d["start_block"]) * C.SECONDS_PER_BLOCK / 60.0
    ax.axvspan(0, span_end, color=C.COL["episode"], alpha=0.12, lw=0)
    ax.plot(d["mins"], d["nt"], color=C.COL["nt"], lw=1.0)
    ax.axhline(d["threshold"], color=C.COL["threshold"], ls="--", lw=0.9)

    # peak n(t) within the cascade span (the alarm saturates during the cascade)
    in_span = (d["mins"] >= 0) & (d["mins"] <= span_end)
    peak = d["nt"][in_span].max() if in_span.any() else d["nt"].max()
    ax.text(0.02, 0.88, f"peak n(t) = {peak:.3f}", transform=ax.transAxes,
            fontsize=7.5, color=C.COL["nt"], fontweight="bold",
            bbox=dict(facecolor="white", edgecolor="none", alpha=0.8, pad=1))
    ax.set_title(f"{C.PROTO_LABEL[d['protocol']]} · {d['name']}", fontsize=9)
    ax.text(0.02, 0.04, d["fitted"], transform=ax.transAxes, fontsize=6.8,
            color=C.COL["muted"])
    ax.set_ylim(0, 1.03)
    ax.set_xlabel("minutes from cascade start", fontsize=7.5)
    ax.set_ylabel("n(t)", fontsize=7.5)
    ax.margins(x=0)


def main() -> None:
    C.apply_style()
    data = [panel_data(p) for p in C.ALL_PROTOCOLS]
    for d in data:
        peak = d["nt"].max()
        print(f"  {d['protocol']:12} {d['name']:<18} thr={d['threshold']:.4f} "
              f"peak_nt={peak:.4f}")

    fig, axes = plt.subplots(2, 2, figsize=(11, 6.6))
    for ax, d in zip(axes.ravel(), data):
        _plot(ax, d)
    handles = [
        plt.Line2D([], [], color=C.COL["nt"], lw=1.2, label="n(t) branching ratio"),
        plt.Line2D([], [], color=C.COL["threshold"], ls="--", lw=1,
                   label="alarm level (calibrated for Aave; 99.9th-pct for others)"),
        plt.matplotlib.patches.Patch(color=C.COL["episode"], alpha=0.3, label="cascade span"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=3, fontsize=7.8,
               bbox_to_anchor=(0.5, -0.02))
    fig.suptitle("Cross-protocol transfer: n(t) rises into the largest cascade of "
                 "every protocol", fontsize=11, fontweight="bold")
    fig.tight_layout(rect=(0, 0.03, 1, 0.96))
    C.save(fig, "fig8_cross_protocol")


if __name__ == "__main__":
    main()
