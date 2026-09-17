#!/usr/bin/env python3
"""Precompute + persist each protocol's `<= 1 FA/week` live alert threshold.

`experiments/E3/run_hawkes_eval.py` already computes this per protocol (the
`recall_at_far_budget` call at its headline horizon, printed as
"threshold=..." to stdout) but never writes the threshold value itself into
`e3_hawkes_eval.json` -- only the resulting per-episode detection table. The
live monitor needs the number itself as its alert trigger, so this script
reuses the exact same library calls `run_hawkes_eval.py` does (no changes to
that script or to `models/hawkes.py`) and writes the result to
`data/live_state/thresholds.json`, which `live/monitor.py` reads at startup.

This is a walk-forward OOF fit (same cost as one `run_hawkes_eval.py`
protocol run, a few minutes) -- run it once, not on every daemon restart.

Usage:
 python scripts/live/calibrate_thresholds.py
 python scripts/live/calibrate_thresholds.py --protocol aave_v3
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve.parents[2] / "src"))

import numpy as np # noqa: E402
import pandas as pd # noqa: E402

from cascadesignal.eval.alerts import recall_at_far_budget # noqa: E402
from cascadesignal.eval.splitter import WalkForwardSplitter # noqa: E402
from cascadesignal.labels.cascade_labeler import load_liquidations # noqa: E402
from cascadesignal.live.config import DEFAULT_BAR_BLOCKS # noqa: E402
from cascadesignal.models.harness import walk_forward_oof_scores # noqa: E402
from cascadesignal.models.hawkes import make_operating_model # noqa: E402
from cascadesignal.models.labels import build_liquidation_bars, make_bar_labels # noqa: E402

PROTOCOLS = ("aave_v2", "aave_v3")
HEADLINE_HORIZON = 50 # matches run_hawkes_eval.py's DEFAULT_HORIZONS[0]
# Sourced from live/config.py rather than hardcoded: mu/alpha/beta are per-bar
# units, so a threshold calibrated at one bar width is meaningless at another.
BAR_BLOCKS = DEFAULT_BAR_BLOCKS
FAR_BUDGET_PER_WEEK = 1.0
OUT_PATH = Path("data/live_state/thresholds.json")


def _blocks_per_week(bars: pd.DataFrame) -> float:
 t = pd.to_datetime(bars["end_time"], utc=True)
 weeks = (t.max - t.min) / pd.Timedelta(weeks=1)
 span = int(bars["end_block"].max - bars["end_block"].min)
 return span / weeks


def _far_budget_threshold_grid(scores: np.ndarray, n: int = 50_000) -> np.ndarray:
 uniq = np.unique(scores[np.isfinite(scores)])
 if len(uniq) <= n:
 return uniq
 idx = np.linspace(0, len(uniq) - 1, n).round.astype(np.int64)
 return uniq[idx]


def calibrate(protocol: str, far_budget: float = FAR_BUDGET_PER_WEEK) -> dict:
 liq = load_liquidations(protocols=[protocol])
 bars = build_liquidation_bars(liq, bar_blocks=BAR_BLOCKS)
 episodes_path = Path("data/curated/labels") / protocol / "episodes.parquet"
 episodes = pd.read_parquet(episodes_path)
 primary_all = episodes[episodes["is_primary"]].copy

 y = make_bar_labels(bars, episodes, HEADLINE_HORIZON, primary_only=True)
 splitter = WalkForwardSplitter(min_train_months=6)
 oof, scored = walk_forward_oof_scores(
 make_operating_model, bars, y, splitter
 )
 scores = oof[scored]
 blocks = bars["end_block"].to_numpy(dtype=np.int64)[scored]
 lo, hi = blocks.min, blocks.max
 primary = primary_all[
 (primary_all["start_block"] >= lo) & (primary_all["start_block"] <= hi)
 ].reset_index(drop=True)
 bpw = _blocks_per_week(bars)

 res = recall_at_far_budget(
 scores,
 blocks,
 primary,
 HEADLINE_HORIZON,
 far_budget,
 bpw,
 thresholds=_far_budget_threshold_grid(scores),
 )
 return {
 "protocol": protocol,
 "bar_blocks": BAR_BLOCKS,
 "horizon_blocks": HEADLINE_HORIZON,
 "far_budget_per_week": far_budget,
 "threshold": res.threshold,
 "achieved_far_per_week": res.achieved_far_per_week,
 "recall": res.recall,
 "feasible": res.feasible,
 # What fraction of bars this operating point puts in alarm -- the cost
 # side of the tradeoff, alongside `recall` as the benefit side.
 "frac_bars_alarming": float((scores >= res.threshold).mean),
 "n_episodes_in_range": int(len(primary)),
 "calibration_note": (
 f"Calibrated against only {len(primary)} historical episode(s) in "
 "range -- thin (a thin-calibration caveat). Not a "
 "statistically robust threshold."
 ),
 }


def main -> None:
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument("--protocol", choices=PROTOCOLS, default=None)
 parser.add_argument(
 "--far-budget",
 type=float,
 default=FAR_BUDGET_PER_WEEK,
 help=(
 "false alarms per week to spend (default %(default)s). Raising it "
 "lowers the threshold, which buys earlier warnings at the cost of "
 "more alarms -- explore the tradeoff without editing source."
 ),
 )
 args = parser.parse_args
 protocols = [args.protocol] if args.protocol else list(PROTOCOLS)

 OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
 out = json.loads(OUT_PATH.read_text) if OUT_PATH.exists else {}
 for protocol in protocols:
 print(f"Calibrating {protocol} (bar_blocks={BAR_BLOCKS}, "
 f"far_budget={args.far_budget}/wk)...", flush=True)
 result = calibrate(protocol, far_budget=args.far_budget)
 out[protocol] = result
 print(
 f" {protocol}: threshold={result['threshold']:.4f} "
 f"achieved_far={result['achieved_far_per_week']:.3f}/wk "
 f"recall={result['recall']:.3f} "
 f"bars_alarming={result['frac_bars_alarming']*100:.2f}% "
 f"feasible={result['feasible']} "
 f"(n_episodes={result['n_episodes_in_range']})",
 flush=True,
 )
 OUT_PATH.write_text(json.dumps(out, indent=2, default=str))
 print(f"\nWrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
 main
