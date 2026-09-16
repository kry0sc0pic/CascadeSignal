#!/usr/bin/env python3
"""Fig. 5 -- the money: how much liquidation value the alarm gets ahead of.

Left: for each major cascade, the liquidation USD that had already been
liquidated when the debounced alarm fired (too late to act on) vs. the USD
still ahead of the alarm (a chance to act). Right: aggregated over ALL labelled
cascades, the total `severity_usd` the operating point warned on IN TIME (a
debounced alarm strictly before the cascade began), with a percentile bootstrap
CI over episodes. This is an upper bound -- it assumes a perfect, instant
intervention on every timely alarm -- not a claim the USD was actually saved.

Run: set -a && source .env && set +a && uv run python scripts/paper/fig5_protectable_usd.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import matplotlib.pyplot as plt  # noqa: E402

import _common as C  # noqa: E402

PAD = 3000


def cascade_cost_timing(protocol: str, bars: pd.DataFrame, threshold, k) -> list[dict]:
    """Per major cascade: USD already liquidated at the alarm vs. USD still
    ahead of it, over a window bracketing the episode."""
    all_fires = C.debounced_alarm_blocks(bars["n_t"].to_numpy(),
                                         bars["end_block"].to_numpy(dtype=np.int64),
                                         threshold, k)
    liq = C.load_liquidations(protocols=[protocol])
    prim = C.load_episodes(protocol, primary_only=True).sort_values(
        "severity_usd", ascending=False)
    out = []
    for _, e in prim.iterrows():
        sb, eb = int(e["start_block"]), int(e["end_block"])
        cand = all_fires[(all_fires >= sb - PAD) & (all_fires <= sb + PAD)]
        wl = liq[(liq["block_number"] >= sb - PAD) & (liq["block_number"] <= eb + PAD)]
        usd = wl["amount_usd"].fillna(0.0).to_numpy()
        lblk = wl["block_number"].to_numpy()
        total = float(usd.sum())
        if len(cand):
            fire = int(cand.min())
            ahead = float(usd[lblk >= fire].sum())
        else:
            fire, ahead = None, 0.0
        out.append({
            "protocol": protocol,
            "name": C.EPISODE_NAMES.get((protocol, sb), str(e["start_time"])[:10]),
            "total_usd": total, "ahead_usd": ahead, "behind_usd": total - ahead,
            "frac_ahead": ahead / total if total else 0.0,
        })
    return out


def protectable_aggregate(protocol: str, bars: pd.DataFrame, threshold, k,
                          n_boot: int = 5000, seed: int = 42) -> dict:
    """Total severity_usd over episodes the alarm warned on IN TIME (a debounced
    fire strictly before start), with a bootstrap CI over episodes."""
    fires = C.debounced_alarm_blocks(bars["n_t"].to_numpy(),
                                     bars["end_block"].to_numpy(dtype=np.int64),
                                     threshold, k)
    eps = C.load_episodes(protocol).reset_index(drop=True)
    starts = eps["start_block"].to_numpy(dtype=np.int64)
    sev = eps["severity_usd"].to_numpy(dtype=float)
    # in-time detection: a fire in [start - LEAD_WINDOW, start)
    in_time = np.array([
        bool(np.any((s - C.LEAD_WINDOW <= fires) & (fires < s))) for s in starts
    ])
    total = float(sev.sum())
    point = float(sev[in_time].sum())

    rng = np.random.default_rng(seed)
    n = len(eps)
    boot = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, n, size=n)
        boot[b] = sev[idx][in_time[idx]].sum()
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return {
        "protocol": protocol, "total_usd": total, "protectable_usd": point,
        "ci_low": float(lo), "ci_high": float(hi),
        "frac": point / total if total else 0.0,
        "n_episodes": int(n), "n_in_time": int(in_time.sum()),
    }


def _fmt_usd(x: float) -> str:
    if x >= 1e9:
        return f"${x / 1e9:.2f}B"
    if x >= 1e6:
        return f"${x / 1e6:.0f}M"
    return f"${x / 1e3:.0f}k"


def _plot_timing(ax, timing):
    labels = [f"{d['name']}\n({C.PROTO_LABEL[d['protocol']]})" for d in timing]
    x = np.arange(len(timing))
    behind = np.array([d["behind_usd"] / 1e6 for d in timing])
    ahead = np.array([d["ahead_usd"] / 1e6 for d in timing])
    ax.bar(x, behind, color=C.COL["false"], label="already liquidated at alarm")
    ax.bar(x, ahead, bottom=behind, color=C.COL["alarm"],
           label="still ahead of alarm (actionable)")
    for xi, d in enumerate(timing):
        ax.text(xi, (d["total_usd"]) / 1e6, f"{d['frac_ahead'] * 100:.0f}% ahead",
                ha="center", va="bottom", fontsize=7.5, fontweight="bold",
                color=C.COL["alarm"])
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=7.5)
    ax.set_ylabel("liquidation USD in window ($M)", fontsize=8.5)
    ax.set_title("Per cascade: liquidation value still ahead of the alarm", fontsize=9)
    ax.set_ylim(top=max(d["total_usd"] for d in timing) / 1e6 * 1.18)
    ax.legend(fontsize=7.5, loc="upper center")


def _plot_aggregate(ax, aggs):
    x = np.arange(len(aggs))
    w = 0.6
    for i, a in enumerate(aggs):
        ax.bar(i, a["total_usd"] / 1e9, w, color=C.COL["grid"], edgecolor="#999",
               label="total cascade USD" if i == 0 else None)
        yerr = [[(a["protectable_usd"] - a["ci_low"]) / 1e9],
                [(a["ci_high"] - a["protectable_usd"]) / 1e9]]
        ax.bar(i, a["protectable_usd"] / 1e9, w, color=C.SERIES[0],
               label="warned in time (protectable)" if i == 0 else None)
        ax.errorbar(i, a["protectable_usd"] / 1e9, yerr=yerr, fmt="none",
                    ecolor="black", capsize=4, lw=1)
        ax.text(i, a["total_usd"] / 1e9,
                f"{_fmt_usd(a['protectable_usd'])} / {_fmt_usd(a['total_usd'])}\n"
                f"{a['frac'] * 100:.0f}% · {a['n_in_time']}/{a['n_episodes']} episodes",
                ha="center", va="bottom", fontsize=7.5)
    ax.set_xticks(x)
    ax.set_xticklabels([C.PROTO_LABEL[a["protocol"]] for a in aggs])
    ax.set_ylabel("USD ($B)", fontsize=8.5)
    ax.set_title("Protectable USD over all labelled cascades (95% bootstrap CI)",
                 fontsize=9)
    ax.legend(fontsize=7.5, loc="center left")
    ax.set_ylim(0, max(a["total_usd"] for a in aggs) / 1e9 * 1.22)


def main() -> None:
    C.apply_style()
    timing, aggs = [], []
    for p in C.LIVE_PROTOCOLS:
        thr, k, _ = C.operating_point(p)
        bars = C.scored_bars(p)
        timing.extend(cascade_cost_timing(p, bars, thr, k))
        aggs.append(protectable_aggregate(p, bars, thr, k))
    for d in timing:
        print(f"  {d['protocol']} {d['name']}: {d['frac_ahead']*100:.0f}% ahead "
              f"({_fmt_usd(d['ahead_usd'])} of {_fmt_usd(d['total_usd'])})")
    for a in aggs:
        print(f"  {a['protocol']}: protectable {_fmt_usd(a['protectable_usd'])} / "
              f"{_fmt_usd(a['total_usd'])} = {a['frac']*100:.0f}% "
              f"[{_fmt_usd(a['ci_low'])}, {_fmt_usd(a['ci_high'])}]")

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))
    _plot_timing(axes[0], timing)
    _plot_aggregate(axes[1], aggs)
    fig.suptitle("Economic reach: liquidation value the alarm gets ahead of",
                 fontsize=11, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    C.save(fig, "fig5_protectable_usd")
    (C.FIG_DIR / "fig5_protectable_usd.json").write_text(
        json.dumps({"timing": timing, "aggregate": aggs}, indent=2))


if __name__ == "__main__":
    main()
