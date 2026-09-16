#!/usr/bin/env python3
"""Tune the alert threshold to MINIMIZE false alarms while PRESERVING early
warning on the existing (primary) cascades -- early warning is the priority.

False alarms fall monotonically as the threshold rises, but so does the lead
(the crossing moves later), and eventually a cascade drops out of its lead
window entirely. So this reports the tradeoff and two operating points:

  * lead-preserving : the highest threshold at which NO currently-detected
    primary episode loses any lead. This is the false-alarm cut you get for
    free -- early warning is exactly preserved. (RECOMMENDED, matches the
    stated priority.)
  * max-cut         : the highest threshold that still detects the same set of
    episodes (recall unchanged) but lets the lead shrink toward its floor --
    a bigger false-alarm cut if you'll trade some warning time.

Everything is measured with the live params (data/live_state/<p>.json) and the
same false-alarm accounting as eval_false_alarms.py. Nothing is applied; the
persisted operating point is untouched. Run:
    python scripts/live/tune_threshold.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cascadesignal.eval.alerts import classify_alerts, raise_alert_events  # noqa: E402
from cascadesignal.labels.cascade_labeler import load_liquidations  # noqa: E402
from cascadesignal.live.state import STATE_DIR, load_state  # noqa: E402
from cascadesignal.models.hawkes import make_operating_model  # noqa: E402
from cascadesignal.models.labels import build_liquidation_bars  # noqa: E402

THRESHOLDS_PATH = STATE_DIR / "thresholds.json"
PROTOCOLS = ("aave_v2", "aave_v3")
SECONDS_PER_WEEK = 7 * 24 * 3600
SECONDS_PER_BLOCK = 12.5


def _blocks_per_week(bars: pd.DataFrame) -> float:
    t = pd.to_datetime(bars["end_time"], utc=True)
    span = int(bars["end_block"].iloc[-1] - bars["end_block"].iloc[0])
    return span / ((t.iloc[-1] - t.iloc[0]).total_seconds() / SECONDS_PER_WEEK)


def tune_protocol(protocol: str, thresholds: dict) -> dict | None:
    state = load_state(protocol)
    tcfg = thresholds.get(protocol)
    if state is None or tcfg is None:
        print(f"  [skip {protocol}]")
        return None
    cur = tcfg["threshold"]
    lead = tcfg["horizon_blocks"]

    liq = load_liquidations(protocols=[protocol])
    bars = build_liquidation_bars(liq, bar_blocks=state.bar_blocks)
    model = make_operating_model()  # ADR-007 USD-marked blend
    model._params = (state.mu, state.alpha, state.beta)  # noqa: SLF001
    model._excite_scale = getattr(state, "excite_scale", 1.0)  # noqa: SLF001
    model._r_end = 0.0  # noqa: SLF001
    scores = model.score(bars)
    blocks = bars["end_block"].to_numpy()

    all_ep = pd.read_parquet(Path("data/curated/labels") / protocol / "episodes.parquet")
    primary = all_ep[all_ep["is_primary"]].copy()
    bpw = _blocks_per_week(bars)
    weeks = (blocks.max() - blocks.min()) / bpw

    def far(t: float) -> float:
        alerts = raise_alert_events(scores, blocks, t)
        classified, _ = classify_alerts(alerts, all_ep, lead)
        return int(classified["is_false_alarm"].sum()) / weeks

    # per primary episode at the current threshold
    episodes = []
    v0s, ceilings = [], []
    for _, e in primary.iterrows():
        start = int(e["start_block"])
        w = (blocks >= start - lead) & (blocks < start)
        peak = float(scores[w].max()) if w.any() else 0.0
        cross = blocks[w & (scores >= cur)]
        detected = len(cross) > 0
        cb = int(cross.min()) if detected else None
        v0 = float(scores[blocks == cb][0]) if detected else None
        episodes.append(
            {
                "date": str(e["start_time"])[:10],
                "peak_in_window": peak,
                "detected_at_current": detected,
                "lead_blocks": (start - cb) if detected else None,
            }
        )
        if detected:
            v0s.append(v0)
            ceilings.append(peak)

    lead_preserving = min(v0s) if v0s else cur
    max_cut = min(ceilings) if ceilings else cur

    def leads_at(t: float):
        out = []
        for _, e in primary.iterrows():
            start = int(e["start_block"])
            w = (blocks >= start - lead) & (blocks < start)
            cross = blocks[w & (scores >= t)]
            out.append((start - int(cross.min())) if len(cross) else None)
        return out

    # frontier over the n(t) values actually present in the detected windows
    vals = set()
    for _, e in primary.iterrows():
        start = int(e["start_block"])
        w = (blocks >= start - lead) & (blocks < start)
        vals.update(float(v) for v in scores[w] if v >= cur)
    grid = sorted(vals)
    if len(grid) > 14:
        grid = [grid[i] for i in np.linspace(0, len(grid) - 1, 14).astype(int)]
    frontier = []
    for t in grid:
        ld = leads_at(t)
        det = [x for x in ld if x is not None]
        frontier.append(
            {
                "threshold": t,
                "recall": len(det) / len(primary),
                "min_lead_blocks": min(det) if det else None,
                "far_per_week": far(t),
            }
        )

    return {
        "protocol": protocol,
        "lead_blocks_horizon": lead,
        "current": {"threshold": cur, "far_per_week": far(cur), "leads_blocks": leads_at(cur)},
        "recommended": {
            "lead_preserving": {
                "threshold": lead_preserving,
                "far_per_week": far(lead_preserving),
                "leads_blocks": leads_at(lead_preserving),
            },
            "max_cut": {
                "threshold": max_cut,
                "far_per_week": far(max_cut),
                "leads_blocks": leads_at(max_cut),
            },
        },
        "episodes": episodes,
        "frontier": frontier,
    }


def _report(r: dict) -> None:
    print(f"\n=== {r['protocol']}  (lead horizon {r['lead_blocks_horizon']}b) ===")
    print("  primary episodes at current threshold:")
    for e in r["episodes"]:
        det = (
            f"lead {e['lead_blocks']}b (~{e['lead_blocks']*SECONDS_PER_BLOCK/60:.0f}m)"
            if e["detected_at_current"]
            else f"NOT WARNED (window peak {e['peak_in_window']:.5f})"
        )
        print(f"    {e['date']}: {det}")
    c = r["current"]
    lp = r["recommended"]["lead_preserving"]
    mc = r["recommended"]["max_cut"]

    def pct(a, b):
        return f"{(1 - b / a) * 100:.0f}% fewer" if a else "n/a"

    print(f"\n  current        : thr={c['threshold']:.5f}  FAR={c['far_per_week']:.3f}/wk")
    print(
        f"  lead-preserving: thr={lp['threshold']:.5f}  FAR={lp['far_per_week']:.3f}/wk "
        f"({pct(c['far_per_week'], lp['far_per_week'])})  leads unchanged: {lp['leads_blocks']}"
    )
    print(
        f"  max-cut        : thr={mc['threshold']:.5f}  FAR={mc['far_per_week']:.3f}/wk "
        f"({pct(c['far_per_week'], mc['far_per_week'])})  leads shrink to: {mc['leads_blocks']}"
    )
    print(f"\n  {'threshold':>10} {'recall':>7} {'min_lead':>10} {'FAR/wk':>8}")
    for f in r["frontier"]:
        ml = (
            f"{f['min_lead_blocks']}b"
            if f["min_lead_blocks"] is not None
            else "-"
        )
        print(f"  {f['threshold']:>10.5f} {f['recall']:>7.2f} {ml:>10} {f['far_per_week']:>8.3f}")


def main() -> None:
    thresholds = json.loads(THRESHOLDS_PATH.read_text())
    results = []
    for p in PROTOCOLS:
        print(f"tuning {p} (scoring full history with live params)...")
        r = tune_protocol(p, thresholds)
        if r:
            results.append(r)
            _report(r)
    out = STATE_DIR / "threshold_tune.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {out}  (nothing applied to the live operating point)")


if __name__ == "__main__":
    main()
