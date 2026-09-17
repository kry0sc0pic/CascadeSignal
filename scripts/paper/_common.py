"""Shared plumbing + house style for the CascadeSignal paper figures.

Every figure in `scripts/paper/` scores the SAME Hawkes branching-ratio n(t)
the live monitor runs, at the SAME operating point (threshold + persistence
filter k), and marks alarms with the SAME debounce the deployed system uses
so the figures describe the actual system, not a rederived one. The operating
point resolves exactly as `live/config.py` and `build_historical_cascades.py`
resolve it: `LIVE_<P>_THRESHOLD` / `LIVE_<P>_DEBOUNCE_K` env overrides win,
else `data/live_state/thresholds.json` / default. Run figures with the env
sourced (`set -a && source .env && set +a`) so those overrides apply.

Not a general library -- just the handful of helpers the figure scripts share.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_SRC = Path(__file__).resolve.parents[2] / "src"
if str(_SRC) not in sys.path:
 sys.path.insert(0, str(_SRC))

from cascadesignal.labels.cascade_labeler import load_liquidations # noqa: E402
from cascadesignal.live.config import ( # noqa: E402
 DEFAULT_DEBOUNCE_WINDOW as DEBOUNCE_WINDOW,
)
from cascadesignal.live.state import STATE_DIR, load_state # noqa: E402
from cascadesignal.models.hawkes import ( # noqa: E402
 HawkesUnivariateBranchingRatio,
 make_operating_model,
)
from cascadesignal.models.labels import build_liquidation_bars # noqa: E402

THRESHOLDS_PATH = STATE_DIR / "thresholds.json"
LABELS_DIR = Path("data/curated/labels")
FIG_DIR = Path("paper/figures")

SECONDS_PER_BLOCK = 12.5 # Ethereum post-merge; blocks -> wall time for copy.
SECONDS_PER_WEEK = 7 * 24 * 3600

# Paper-wide detection rule (documented in captions.md): an alarm counts as a
# true warning of an episode if it fires anywhere in [start - LEAD_WINDOW, end]
# -- crediting early warning AND firing during the cascade -- and a false alarm
# is a fire outside EVERY labelled cascade's window. This is more permissive
# than eval/alerts.py's strict [start-50, start) forecast window (which counts
# an early alarm as "too early" -> false); it is the right accounting for an
# early-warning claim, at the cost of being a modelling choice, not the native
# horizon. 1200 blocks ~= 4.2 h, comfortably covering the observed <=213 min leads.
LEAD_WINDOW = 1200

# Protocols with a persisted live fit + calibrated threshold + USD-priced
# liquidations -- the ones the operating-point / economics figures cover.
LIVE_PROTOCOLS = ("aave_v2", "aave_v3")
# All labelled protocols (compound_v2 / maker have counts only, no USD, no
# persisted fit -- used only in the cross-protocol transfer figure).
ALL_PROTOCOLS = ("aave_v2", "aave_v3", "compound_v2", "maker")

# Short, human labels for the primary episodes, keyed by (protocol, start_block).
# Mirrors build_historical_cascades.EPISODE_LABELS.
EPISODE_NAMES: dict[tuple[str, int], str] = {
 ("aave_v2", 12464836): "May 2021 crash",
 ("aave_v2", 13737970): "Dec 2021 crash",
 ("aave_v2", 14959193): "Jun 2022 (Terra/3AC)",
 ("aave_v3", 21762804): "Feb 2025 crash",
}

PROTO_LABEL = {
 "aave_v2": "Aave v2",
 "aave_v3": "Aave v3",
 "compound_v2": "Compound v2",
 "maker": "Maker",
}


# --------------------------------------------------------------------------- #
# House style (light, print-friendly; the site theme is dark, papers are not).
# --------------------------------------------------------------------------- #
COL = {
 "nt": "#2b6cb0", # branching ratio n(t)
 "nt_fill": "#bcd3ef",
 "cost": "#c05621", # liquidation cost / volume
 "cost_fill": "#f6c99a",
 "threshold": "#c0392b", # alert threshold (dashed)
 "alarm": "#1f9d55", # debounced alarm fire
 "false": "#c0392b", # false alarm
 "episode": "#6b7280", # cascade span shading
 "grid": "#d9dee6",
 "muted": "#6b7280",
}
# A small qualitative ramp for per-model / per-protocol series.
SERIES = ["#2b6cb0", "#c05621", "#1f9d55", "#8046cc", "#00868b", "#b8860b"]


def apply_style -> None:
 import matplotlib as mpl

 mpl.rcParams.update({
 "figure.dpi": 120,
 "savefig.dpi": 300,
 "savefig.bbox": "tight",
 "font.size": 9,
 "font.family": "sans-serif",
 "font.sans-serif": ["DejaVu Sans", "Arial", "Helvetica"],
 "axes.titlesize": 10,
 "axes.titleweight": "bold",
 "axes.labelsize": 9,
 "axes.edgecolor": "#333333",
 "axes.linewidth": 0.8,
 "axes.grid": True,
 "axes.axisbelow": True,
 "grid.color": COL["grid"],
 "grid.linewidth": 0.6,
 "legend.fontsize": 8,
 "legend.frameon": False,
 "xtick.labelsize": 8,
 "ytick.labelsize": 8,
 "figure.facecolor": "white",
 "axes.facecolor": "white",
 })


def save(fig, name: str) -> Path:
 """Write both a vector PDF and a 300-dpi PNG under paper/figures/."""
 FIG_DIR.mkdir(parents=True, exist_ok=True)
 pdf = FIG_DIR / f"{name}.pdf"
 png = FIG_DIR / f"{name}.png"
 fig.savefig(pdf)
 fig.savefig(png)
 print(f" wrote {pdf} + {png}")
 return pdf


# --------------------------------------------------------------------------- #
# Data / scoring
# --------------------------------------------------------------------------- #
def operating_point(protocol: str) -> tuple[float | None, int, int]:
 """(threshold, debounce_k, horizon_blocks) resolved exactly as the live
 monitor resolves them (config.py): env override wins, else thresholds.json.
 """
 p = protocol.upper
 thresholds = json.loads(THRESHOLDS_PATH.read_text) if THRESHOLDS_PATH.exists else {}
 tcfg = thresholds.get(protocol, {})
 thr_env = os.environ.get(f"LIVE_{p}_THRESHOLD")
 threshold = float(thr_env) if thr_env else tcfg.get("threshold")
 debounce_k = int(os.environ.get(f"LIVE_{p}_DEBOUNCE_K", "1"))
 horizon = int(tcfg.get("horizon_blocks", 50))
 return threshold, debounce_k, horizon


def load_episodes(protocol: str, primary_only: bool = False) -> pd.DataFrame:
 df = pd.read_parquet(LABELS_DIR / protocol / "episodes.parquet")
 if primary_only:
 df = df[df["is_primary"]]
 return df.reset_index(drop=True)


def scored_bars(protocol: str, bar_blocks: int | None = None) -> pd.DataFrame:
 """Full-history bars with an `n_t` column, scored EXACTLY as the live
 monitor / Historical tab do: the persisted live fit (mu, alpha, beta),
 scored from the start of history (`_r_end = 0`). Mirrors
 build_historical_cascades._scored_bars. Raises if there is no persisted fit
 (use `fresh_scored_bars` for protocols the monitor never fit)."""
 state = load_state(protocol)
 if state is None:
 raise SystemExit(
 f"no persisted live fit for {protocol!r}; run the live monitor once "
 "or use fresh_scored_bars."
 )
 bb = bar_blocks or state.bar_blocks
 liq = load_liquidations(protocols=[protocol])
 bars = build_liquidation_bars(liq, bar_blocks=bb)
 model = make_operating_model # ADR-007 USD-marked blend
 model._params = (state.mu, state.alpha, state.beta) # noqa: SLF001
 model._r_end = 0.0 # noqa: SLF001
 model._excite_scale = getattr(state, "excite_scale", 1.0) # noqa: SLF001
 return bars.assign(n_t=model.score(bars))


def fresh_scored_bars(
 protocol: str, bar_blocks: int = 1
) -> tuple[pd.DataFrame, HawkesUnivariateBranchingRatio]:
 """Full-history bars scored by a fresh full-sample MLE fit -- for protocols
 with no persisted live fit (compound_v2 / maker). This is a full-sample fit
 (not walk-forward), so it is ILLUSTRATIVE of the signal, not an honest
 operating point; label figures that use it accordingly."""
 liq = load_liquidations(protocols=[protocol])
 bars = build_liquidation_bars(liq, bar_blocks=bar_blocks)
 model = HawkesUnivariateBranchingRatio
 model.fit(bars)
 model._r_end = 0.0 # noqa: SLF001 -- score the whole history from the start
 return bars.assign(n_t=model.score(bars)), model


def blocks_per_week(bars: pd.DataFrame) -> float:
 t = pd.to_datetime(bars["end_time"], utc=True)
 span_blocks = int(bars["end_block"].iloc[-1] - bars["end_block"].iloc[0])
 span_seconds = (t.iloc[-1] - t.iloc[0]).total_seconds
 return span_blocks / (span_seconds / SECONDS_PER_WEEK)


# --------------------------------------------------------------------------- #
# Alarm accounting (debounced, matching the live monitor's persistence filter)
# --------------------------------------------------------------------------- #
def debounced_fire_mask(
 scores: np.ndarray, threshold: float, k: int, window: int = DEBOUNCE_WINDOW
) -> np.ndarray:
 """Boolean mask, True at each bar where the tolerant debounce (ADR-008)
 first fires: a rising edge of "at least `k` above-threshold bars within the
 last `window` bars" -- one True per sustained run. `window == k` reproduces
 the old strict-consecutive behaviour; `window > k` tolerates dips in a
 flickering near-critical run-up (fires earlier AND spawns fewer distinct
 false alarms, since it stays in-alarm through dips). k=1 == rising edge.
 Vectorized so the threshold sweeps (Figs 3/4/6) stay fast over 13 M bars.
 """
 above = np.asarray(scores, dtype=float) >= threshold
 k = max(1, k)
 window = max(window, k)
 # count of above-threshold bars in the trailing `window`-bar window
 pref = np.concatenate(([0], np.cumsum(above.astype(np.int64))))
 idx = np.arange(len(above))
 lo = np.maximum(0, idx - window + 1)
 count = pref[idx + 1] - pref[lo]
 cond = count >= k
 return cond & ~np.concatenate(([False], cond[:-1])) # rising edge of the run


def debounced_alarm_blocks(
 scores: np.ndarray,
 blocks: np.ndarray,
 threshold: float | None,
 k: int,
 window: int = DEBOUNCE_WINDOW,
) -> np.ndarray:
 """end_block of each debounced alarm fire (see `debounced_fire_mask`)."""
 if threshold is None:
 return np.empty(0, dtype=np.int64)
 blocks = np.asarray(blocks, dtype=np.int64)
 return blocks[debounced_fire_mask(scores, threshold, k, window)]


def classify_fires(
 fire_blocks: np.ndarray, episodes: pd.DataFrame, lead_window: int = LEAD_WINDOW
) -> dict:
 """Split debounced fires into true warnings vs false alarms under the paper
 detection rule: a fire is a true warning if it lands in some episode's
 window [start - lead_window, end] (early OR during the cascade); a false
 alarm is a fire in NO episode's window. Classifies against ALL labelled
 episodes, so an alarm near a real (even secondary) cascade is not counted
 false. Returns the true/false fire blocks and the set of detected episode
 indices."""
 starts = episodes["start_block"].to_numpy(dtype=np.int64)
 ends = episodes["end_block"].to_numpy(dtype=np.int64)
 lo = starts - lead_window
 true_mask = np.zeros(len(fire_blocks), dtype=bool)
 detected: set[int] = set
 for i, b in enumerate(fire_blocks):
 hit = np.where((lo <= b) & (b <= ends))[0]
 if len(hit):
 true_mask[i] = True
 detected.update(hit.tolist) # every episode whose window covers b
 return {
 "true_blocks": fire_blocks[true_mask],
 "false_blocks": fire_blocks[~true_mask],
 "detected_idx": detected,
 }
