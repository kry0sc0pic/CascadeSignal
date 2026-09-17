"""Walk-forward evaluation of the Hawkes branching-ratio alarm (E3).

Scores the univariate Hawkes n(t) against the primary D-A cascade episodes,
leakage-free (expanding-window monthly retrain, out-of-fold scoring only), and
reports:

 1. AUPRC lift over prevalence, per forecast horizon (the deck's headline).
 2. The lead-time frontier: best recall at precision floor, per horizon.
 3. A NAMED per-episode detection table at <= 1 false alarm / week.

Point 3 is deliberate. The binding constraint here is episode scarcity, not
data volume -- only a handful of primary episodes fall in the scored range, so
a bare recall fraction (e.g. "0.50") is misleading. Every episode is listed by
name with its own detected/missed outcome and the lead time actually achieved.

Marks are `n_liquidations` per liquidation bar (`build_liquidation_bars`,
fixed-block-width bins of the raw liquidation stream -- see
`models/labels.py`). No DefiLlama, no price oracle -- the Hawkes alarm reads
only the liquidation event stream, so it runs on any protocol with a
liquidation stream, not just Aave v2.

Compound v2 and Maker liquidations carry no `amount_usd`, so the D-A cascade
labeler's severity gate (`total_usd >= theta_usd`) is inert for them
`theta_usd` is the percentile of an all-zero column, i.e. 0, so every
candidate window passes it trivially. Their episodes are breadth+contagion
gated only, not the full D-A definition; this script flags that prominently
rather than silently treating them as equivalent to Aave v2/v3.

--graph-baseline (Aave v2 only) folds the contagion-graph fragility covariate
into the alarm (see graph/fragility.py and models/hawkes.py's `cov_col`).
--graph-mode picks where: `excitation` (default) adds it as cascade pressure in
the n(t) numerator so fragility RAISES the alarm; `baseline` puts it in the
background rate mu*exp(gamma*z) -- the literal "base event rate" form, but it
lowers n(t). Each writes to its own `aave_v2_graph_<mode>` dir so the
constant-mu run stays as the comparison reference.

Usage:
 python experiments/E3/run_hawkes_eval.py
 python experiments/E3/run_hawkes_eval.py --protocol aave_v3
 python experiments/E3/run_hawkes_eval.py --horizons 50 --min-train-months 6
 python experiments/E3/run_hawkes_eval.py --protocol aave_v2 --graph-baseline
 python experiments/E3/run_hawkes_eval.py --protocol aave_v2 --graph-baseline \
 --graph-mode baseline
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from cascadesignal.eval.alerts import classify_alerts, raise_alert_events
from cascadesignal.eval.splitter import EPISODE_HOLDOUTS, WalkForwardSplitter
from cascadesignal.graph.fragility import COV_COL, build_fragility_covariate
from cascadesignal.labels.cascade_labeler import load_liquidations
from cascadesignal.models.harness import baseline_report, walk_forward_oof_scores
from cascadesignal.models.hawkes import HawkesUnivariateBranchingRatio
from cascadesignal.models.labels import build_liquidation_bars, make_bar_labels

PROTOCOLS = ("aave_v2", "aave_v3", "compound_v2", "maker")
# Compound v2 / Maker liquidations have NULL amount_usd (see module docstring).
DEGENERATE_SEVERITY_PROTOCOLS = frozenset({"compound_v2", "maker"})

DATA_DIR = Path("data/raw")
LABELS_DIR = Path("data/curated/labels")
GRAPH_DIR = Path("data/curated/graph")
OUT_DIR = Path("experiments/E3/output")
# Only Aave v2 has a materialized contagion graph (state reconstruction is
# Aave-v2-specific); the graph baseline runs on it alone.
GRAPH_PROTOCOL = "aave_v2"

DEFAULT_HORIZONS = [50, 100, 300]
# Per-protocol bar width, not shared -- both tuned by sweep against the
# h=50 headline horizon, capped at or below it. Once bar_blocks exceeds
# horizon_blocks, two problems compound: n_positive can collapse to a
# handful of bars (Maker's widths past 50 hit 1-4 positives and inflated
# AUPRC lift by 10-30x on sampling noise, not real improvement), and a
# genuine coverage gap opens up -- consecutive bars end up more than
# horizon_blocks apart, so an episode start landing in that gap is missed
# by every bar's forecast window. Both protocols were swept up to and past
# their horizon to see this failure mode directly (see the 2026-08 tuning
# pass); the values below are the best result found at or under the
# horizon, not the best result found anywhere in the sweep.
# - Maker: bb=25 essentially ties bb=50's lift (241.9x vs 251.3x, both 2/8
# detected) with 2x the positive sample size (16 vs 8) -- the safer pick.
# - Compound v2: bb=40 has the best detection count in the whole sweep
# (6/59 vs baseline's 2/59) at a strong lift (51.0x vs 32.2x baseline)
# and a comfortable n_positive=77; bb=50 edges it on lift alone (61.0x)
# but with less margin (n_positive=59).
# Aave v2/v3 haven't been swept yet; keep the aave_v2-tuned default.
DEFAULT_BAR_BLOCKS_BY_PROTOCOL: dict[str, int] = {"maker": 25, "compound_v2": 40}
DEFAULT_BAR_BLOCKS = 5
FAR_BUDGET_PER_WEEK = 1.0


def _blocks_per_week(bars: pd.DataFrame) -> float:
 """Empirical block rate from the bars themselves, not an assumed constant."""
 t = pd.to_datetime(bars["end_time"], utc=True)
 weeks = (t.max - t.min) / pd.Timedelta(weeks=1)
 span = int(bars["end_block"].max - bars["end_block"].min)
 return span / weeks


def _episode_name(start_time: pd.Timestamp) -> str:
 """Name a primary episode after a golden holdout window if it falls in one,
 else by its start date."""
 for h in EPISODE_HOLDOUTS:
 if h.test_start <= start_time < h.test_end:
 return h.name
 return start_time.strftime("%Y-%m-%d")


def _per_episode_table(
 scores: np.ndarray,
 blocks: np.ndarray,
 primary: pd.DataFrame,
 threshold: float,
 lead_blocks: int,
) -> pd.DataFrame:
 """One row per in-range primary episode: detected/missed at `threshold`,
 and the earliest lead time achieved inside its lead window."""
 alert_blocks = raise_alert_events(scores, blocks, threshold)
 classified, detected = classify_alerts(alert_blocks, primary, lead_blocks)

 rows = []
 starts = primary["start_block"].to_numpy(dtype=np.int64)
 times = pd.to_datetime(primary["start_time"], utc=True).to_numpy
 for i in range(len(primary)):
 hits = classified[
 classified["is_true_detection"] & (classified["episode_index"] == i)
 ]
 if len(hits):
 earliest = int(hits["block"].min)
 lead = int(starts[i]) - earliest
 else:
 lead = None
 rows.append(
 {
 "episode": _episode_name(pd.Timestamp(times[i])),
 "start_block": int(starts[i]),
 "start_time": pd.Timestamp(times[i]).strftime("%Y-%m-%d %H:%M"),
 "detected": i in detected,
 "lead_blocks_achieved": lead,
 }
 )
 return pd.DataFrame(rows)


def _far_budget_threshold_grid(scores: np.ndarray, n: int = 50_000) -> np.ndarray:
 """Threshold grid for `recall_at_far_budget`'s per-episode table.

 n(t) is heavily skewed -- a long quiet-period baseline plus a thin tail
 of real signal -- so `harness._threshold_grid`'s quantile-of-the-full-
 sample grid (used for the coarser lead-time-frontier print) spends
 nearly its whole budget resampling the quiet mode and can miss the
 narrow band of thresholds that actually separates real episodes from
 baseline. Sampling evenly by INDEX over the sorted distinct values
 instead gives every distinct value equal weight regardless of how often
 it repeats, so the thin tail gets real coverage. Still bounded well
 below sweeping every one of millions of raw scores (which is what made
 this call intractable before this grid existed).
 """
 uniq = np.unique(scores[np.isfinite(scores)])
 if len(uniq) <= n:
 return uniq
 idx = np.linspace(0, len(uniq) - 1, n).round.astype(np.int64)
 return uniq[idx]


def main -> None:
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument("--protocol", choices=PROTOCOLS, default="aave_v2")
 parser.add_argument("--horizons", type=int, nargs="+", default=DEFAULT_HORIZONS)
 parser.add_argument("--min-train-months", type=int, default=6)
 parser.add_argument(
 "--bar-blocks",
 type=int,
 default=None,
 help=f"Bar width in blocks. Default is per-protocol "
 f"({DEFAULT_BAR_BLOCKS_BY_PROTOCOL}, else {DEFAULT_BAR_BLOCKS}).",
 )
 parser.add_argument(
 "--hawkes-init",
 type=float,
 nargs=3,
 metavar=("MU", "ALPHA", "BETA"),
 default=None,
 help="Override the Hawkes MLE starting point (default: the model's "
 "own (1e-4, 0.5, 0.1), tuned for Aave v2's event rate). Sparser "
 "protocols may fit better from a different starting point -- "
 "parameters are per-protocol, not shared.",
 )
 parser.add_argument(
 "--graph-baseline",
 action="store_true",
 help="Fold the contagion-graph fragility covariate (graph/fragility.py) "
 "into the Hawkes alarm. Aave v2 only -- the one protocol with a "
 "materialized graph. Writes to a separate "
 f"'{GRAPH_PROTOCOL}_graph_<mode>' output dir so the constant-mu "
 "reference is kept for side-by-side comparison.",
 )
 parser.add_argument(
 "--graph-mode",
 choices=("baseline", "excitation"),
 default="excitation",
 help="Where the fragility covariate enters (see models/hawkes.py). "
 "'excitation' (default): fragility is exogenous cascade pressure in "
 "the n(t) numerator, so it RAISES the alarm. 'baseline': log-linear "
 "background rate mu*exp(gamma*z) -- the literal 'base event rate' "
 "form, but it lowers n(t) and cost AUPRC lift on Aave v2 (kept for "
 "the comparison).",
 )
 parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
 parser.add_argument("--labels-dir", type=Path, default=LABELS_DIR)
 parser.add_argument("--graph-dir", type=Path, default=GRAPH_DIR)
 parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
 args = parser.parse_args
 if args.graph_baseline and args.protocol != GRAPH_PROTOCOL:
 parser.error(
 f"--graph-baseline is {GRAPH_PROTOCOL}-only (no materialized "
 f"contagion graph for {args.protocol})"
 )
 bar_blocks = args.bar_blocks
 if bar_blocks is None:
 bar_blocks = DEFAULT_BAR_BLOCKS_BY_PROTOCOL.get(
 args.protocol, DEFAULT_BAR_BLOCKS
 )

 liq = load_liquidations(data_dir=args.data_dir, protocols=[args.protocol])
 bars = build_liquidation_bars(liq, bar_blocks=bar_blocks)
 if args.graph_baseline:
 cov = build_fragility_covariate(
 args.graph_dir / args.protocol / "nodes.parquet"
 )
 # Backward as-of join: each bar carries the most recent snapshot's
 # fragility at or before its as-of cutoff (end_block) -- no look-ahead.
 bars = pd.merge_asof(
 bars,
 cov[["snapshot_block", COV_COL]],
 left_on="end_block",
 right_on="snapshot_block",
 direction="backward",
 ).drop(columns="snapshot_block")
 episodes = pd.read_parquet(args.labels_dir / args.protocol / "episodes.parquet")
 primary_all = episodes[episodes["is_primary"]].copy

 if args.protocol in DEGENERATE_SEVERITY_PROTOCOLS:
 print(
 f"NOTE: {args.protocol} liquidations carry no amount_usd -- the D-A "
 "severity gate is inert (theta_usd=0, always passes). Episodes below "
 "are breadth+contagion gated only, PROVISIONAL, not directly "
 "comparable to aave_v2/aave_v3.",
 flush=True,
 )

 bpw = _blocks_per_week(bars)
 splitter = WalkForwardSplitter(min_train_months=args.min_train_months)
 print(
 f"protocol={args.protocol} bar_blocks={bar_blocks} {len(bars):,} bars, "
 f"{len(primary_all)} primary episodes, {bpw:,.0f} blocks/week",
 flush=True,
 )

 cov_col = COV_COL if args.graph_baseline else None
 cov_mode = args.graph_mode
 if args.hawkes_init is not None:
 init = tuple(args.hawkes_init)
 model_factory = lambda: HawkesUnivariateBranchingRatio( # noqa: E731
 init=init, cov_col=cov_col, cov_mode=cov_mode
 )
 else:
 model_factory = lambda: HawkesUnivariateBranchingRatio( # noqa: E731
 cov_col=cov_col, cov_mode=cov_mode
 )

 graph_cov_coef: float | None = None
 if args.graph_baseline:
 # In-sample diagnostic only: the OOF scores below refit per fold, each
 # with its own coefficient; this single full-history fit is just a
 # headline sign/magnitude. gamma (baseline mode) or delta (excitation).
 graph_cov_coef = float(model_factory.fit(bars).cov_coef)
 coef_name = "delta" if cov_mode == "excitation" else "gamma"
 print(
 f"graph baseline [{cov_mode}]: in-sample {coef_name}="
 f"{graph_cov_coef:+.4f}",
 flush=True,
 )

 headline_h = min(args.horizons)
 report_rows = []
 per_episode: pd.DataFrame | None = None
 n_t_range: tuple[float, float] | None = None

 for h in args.horizons:
 y = make_bar_labels(bars, episodes, h, primary_only=True)
 oof, scored = walk_forward_oof_scores(model_factory, bars, y, splitter)
 rep = baseline_report("Hawkes n(t)", oof, scored, y, bars, episodes, [h])
 rep["horizon_blocks"] = h
 lift = rep.get("auprc_lift_over_prevalence")
 report_rows.append(rep)
 print(
 f"h={h:>3} prevalence={rep['prevalence']:.3e} "
 f"AUPRC={rep['auprc']} lift={lift} "
 f"episodes_in_range={rep['n_episodes_in_range']}",
 flush=True,
 )

 if h == headline_h:
 scores = oof[scored]
 n_t_range = (float(np.min(scores)), float(np.max(scores)))
 blocks = bars["end_block"].to_numpy(dtype=np.int64)[scored]
 lo, hi = blocks.min, blocks.max
 primary = primary_all[
 (primary_all["start_block"] >= lo) & (primary_all["start_block"] <= hi)
 ].reset_index(drop=True)
 # Best recall among thresholds meeting <= 1 FA/week, then the table.
 from cascadesignal.eval.alerts import recall_at_far_budget

 res = recall_at_far_budget(
 scores,
 blocks,
 primary,
 h,
 FAR_BUDGET_PER_WEEK,
 bpw,
 thresholds=_far_budget_threshold_grid(scores),
 )
 per_episode = _per_episode_table(scores, blocks, primary, res.threshold, h)
 print(
 f"\n<= {FAR_BUDGET_PER_WEEK:.0f} FA/week at h={h}: "
 f"threshold={res.threshold:.4f} achieved_far={res.achieved_far_per_week:.3f}/wk "
 f"feasible={res.feasible}",
 flush=True,
 )
 print("Per-episode detection (named, not a bare recall fraction):")
 print(per_episode.to_string(index=False), flush=True)
 n_det = int(per_episode["detected"].sum)
 print(
 f"Detected {n_det} of {len(per_episode)} in-range primary episodes.",
 flush=True,
 )

 out_tag = (
 f"{args.protocol}_graph_{cov_mode}" if args.graph_baseline else args.protocol
 )
 out_dir = args.out_dir / out_tag
 out_dir.mkdir(parents=True, exist_ok=True)
 with open(out_dir / "e3_hawkes_eval.json", "w") as f:
 json.dump(
 {
 "protocol": args.protocol,
 "bar_blocks": bar_blocks,
 "graph_baseline": args.graph_baseline,
 "graph_mode": cov_mode if args.graph_baseline else None,
 "graph_cov_coef": graph_cov_coef,
 "degenerate_severity_gate": args.protocol
 in DEGENERATE_SEVERITY_PROTOCOLS,
 "blocks_per_week": bpw,
 "n_t_range": n_t_range,
 "reports": report_rows,
 "per_episode": (
 per_episode.to_dict(orient="records")
 if per_episode is not None
 else []
 ),
 },
 f,
 indent=2,
 default=str,
 )
 if per_episode is not None:
 per_episode.to_csv(out_dir / "e3_hawkes_per_episode.csv", index=False)
 print(f"\nWrote {out_dir}/e3_hawkes_eval.json", flush=True)


if __name__ == "__main__":
 main
