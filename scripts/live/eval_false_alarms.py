#!/usr/bin/env python3
"""Out-of-sample false-alarm rate for the live operating point.

Takes the SAME parameters the live monitor runs with -- the persisted Hawkes
fit (`data/live_state/<p>.json`: mu, alpha, beta) and the calibrated alert
threshold (`data/live_state/thresholds.json`) -- and measures how often that
operating point raises an alarm during genuinely QUIET periods (no cascade of
any kind), broken down by year.

Why per-year: the calibration `achieved_far_per_week` is measured on the same
thin window the threshold was tuned on (2-3 primary episodes). Every episode
in the labels sits in 2021-2024, so the later years are cascade-free stretches
the operating point never saw -- a clean out-of-sample false-alarm estimate.
A "false alarm" here excludes ALL labelled cascades (not just the primary
ones), so an alarm during a real-but-secondary cascade is not miscounted.

Read-only; nothing is refit or recalibrated. Run:
    python scripts/live/eval_false_alarms.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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


def _blocks_per_week(bars: pd.DataFrame) -> float:
    t = pd.to_datetime(bars["end_time"], utc=True)
    span_blocks = int(bars["end_block"].iloc[-1] - bars["end_block"].iloc[0])
    span_seconds = (t.iloc[-1] - t.iloc[0]).total_seconds()
    return span_blocks / (span_seconds / SECONDS_PER_WEEK)


def eval_protocol(protocol: str, thresholds: dict) -> dict | None:
    state = load_state(protocol)
    tcfg = thresholds.get(protocol)
    if state is None or tcfg is None:
        print(f"  [skip {protocol}: no persisted fit or threshold]")
        return None
    threshold = tcfg["threshold"]
    lead = tcfg["horizon_blocks"]

    liq = load_liquidations(protocols=[protocol])
    bars = build_liquidation_bars(liq, bar_blocks=state.bar_blocks)
    model = make_operating_model()  # ADR-007 USD-marked blend
    model._params = (state.mu, state.alpha, state.beta)  # noqa: SLF001
    model._excite_scale = getattr(state, "excite_scale", 1.0)  # noqa: SLF001
    model._r_end = 0.0  # noqa: SLF001
    scores = model.score(bars)
    blocks = bars["end_block"].to_numpy()

    # Exclude EVERY labelled cascade (all definitions), so "false alarm" means
    # an alarm in a genuinely cascade-free period.
    episodes = pd.read_parquet(Path("data/curated/labels") / protocol / "episodes.parquet")

    alert_blocks = raise_alert_events(scores, blocks, threshold)
    classified, _ = classify_alerts(alert_blocks, episodes, lead)
    false_blocks = set(classified.loc[classified["is_false_alarm"], "block"].tolist())

    bpw = _blocks_per_week(bars)
    year = pd.to_datetime(bars["end_time"], utc=True).dt.year
    year_by_block = dict(zip(bars["end_block"].tolist(), year.tolist()))
    ep_year = pd.to_datetime(episodes["start_time"], utc=True).dt.year.value_counts().to_dict()

    rows = []
    for y in sorted(year.unique()):
        mask = year == y
        n_bars = int(mask.sum())
        weeks = n_bars * state.bar_blocks / bpw
        n_false = sum(1 for b in false_blocks if year_by_block.get(b) == y)
        n_alarm_bars = int((scores[mask.to_numpy()] >= threshold).sum())
        rows.append(
            {
                "year": int(y),
                "weeks": round(weeks, 1),
                "false_alarms": n_false,
                "far_per_week": round(n_false / weeks, 3) if weeks else None,
                "frac_bars_alarming": round(n_alarm_bars / n_bars, 6) if n_bars else 0.0,
                "labelled_cascades": int(ep_year.get(y, 0)),
                "in_sample": ep_year.get(y, 0) > 0,
            }
        )
    total_weeks = len(bars) * state.bar_blocks / bpw
    total_false = len(false_blocks)
    return {
        "protocol": protocol,
        "threshold": threshold,
        "lead_blocks": lead,
        "params": {"mu": state.mu, "alpha": state.alpha, "beta": state.beta},
        "calibration_far_per_week": tcfg.get("achieved_far_per_week"),
        "overall": {
            "weeks": round(total_weeks, 1),
            "total_alerts": int(len(alert_blocks)),
            "false_alarms": total_false,
            "far_per_week": round(total_false / total_weeks, 3),
        },
        "by_year": rows,
    }


def _print_report(r: dict) -> None:
    o = r["overall"]
    print(f"\n=== {r['protocol']} ===")
    print(
        f"operating point: threshold={r['threshold']:.4f} lead={r['lead_blocks']}b "
        f"mu={r['params']['mu']:.2e} alpha={r['params']['alpha']:.3f} beta={r['params']['beta']:.3f}"
    )
    print(
        f"overall: {o['weeks']:.0f} weeks, {o['total_alerts']} alerts, "
        f"{o['false_alarms']} false alarms -> FAR = {o['far_per_week']:.3f}/week "
        f"(calibration reported {r['calibration_far_per_week']:.3f}/week)"
    )
    print(f"{'year':>6} {'weeks':>7} {'false':>6} {'FAR/wk':>8} {'%bars_alarm':>12} {'cascades':>9}  sample")
    for row in r["by_year"]:
        tag = "in-sample" if row["in_sample"] else "OUT-OF-SAMPLE"
        far = f"{row['far_per_week']:.3f}" if row["far_per_week"] is not None else "  -  "
        print(
            f"{row['year']:>6} {row['weeks']:>7.1f} {row['false_alarms']:>6} {far:>8} "
            f"{row['frac_bars_alarming']*100:>11.4f}% {row['labelled_cascades']:>9}  {tag}"
        )
    oos = [r_ for r_ in r["by_year"] if not r_["in_sample"] and r_["weeks"] > 1]
    if oos:
        w = sum(r_["weeks"] for r_ in oos)
        f = sum(r_["false_alarms"] for r_ in oos)
        print(f"  --> pooled OUT-OF-SAMPLE FAR = {f}/{w:.0f}wk = {f / w:.3f}/week")


def main() -> None:
    thresholds = json.loads(THRESHOLDS_PATH.read_text())
    results = []
    for p in PROTOCOLS:
        print(f"scoring {p} full history with its live operating point...")
        r = eval_protocol(p, thresholds)
        if r:
            results.append(r)
            _print_report(r)
    out = STATE_DIR / "false_alarm_eval.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
