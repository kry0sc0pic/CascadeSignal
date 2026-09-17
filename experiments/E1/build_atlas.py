"""E1 -- Cascade atlas + per-episode dossiers.

Reproducibly builds, from the ingested data lake and:
 - experiments/E1/output/atlas.png: timeline of all detected D-A
 primary-grid cascade episodes, Aave v2, 2021 -> Feb 2026.
 - experiments/E1/output/episode_table.csv: full episode table (all 27
 grid points), for reconciliation and downstream use.
 - experiments/E1/output/golden_episode_summary.csv: the 5 golden episodes
 against published Aave figures (PLAN §2) and D-A grid coverage.

Scope: Aave v2 only, matching.
Two of the five golden episodes -- Oct 2025 and Feb 2026 -- are not
reconstructable as cascades from Aave-v2-only data (v2 liquidation activity
by then is negligible; the real activity moved to v3). That is a tracked
follow-up, not solved here -- see
docs/episodes/ for the honest per-episode dossiers.
"""

from __future__ import annotations

import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from cascadesignal.labels.cascade_labeler import load_liquidations

log = logging.getLogger(__name__)

OUT_DIR = Path(__file__).parent / "output"

# (name, window_start, window_end, published_usd_note)
GOLDEN_EPISODES = [
 (
 "China crackdown",
 "2021-05-01",
 "2021-07-01",
 "$362M / 5,500+ events (Aave v2 alone)",
 ),
 (
 "Terra/LUNA",
 "2022-05-01",
 "2022-06-20",
 "32,000+ positions liquidated (platform-wide)",
 ),
 ("FTX collapse", "2022-11-06", "2022-11-11", "not published as a single figure"),
 (
 "Oct 2025 flash deleveraging",
 "2025-10-08",
 "2025-10-13",
 "$250M+ on Aave (platform-wide); ~$31B market-wide",
 ),
 (
 "Feb 2026 Fed-nomination",
 "2026-01-29",
 "2026-02-06",
 "$429M / ~12,500 txs (Aave all-time record, platform-wide)",
 ),
]


def build_atlas(
 labels_path: Path,
 data_dir: Path,
 out_dir: Path = OUT_DIR,
) -> None:
 out_dir.mkdir(parents=True, exist_ok=True)
 episodes = pd.read_parquet(labels_path)
 primary = (
 episodes[episodes["is_primary"]]
 .sort_values("start_time")
 .reset_index(drop=True)
 )
 episodes.to_csv(out_dir / "episode_table.csv", index=False)

 liquidations = load_liquidations(data_dir=data_dir, protocols=("aave_v2",))

 summary_rows = []
 fig, ax = plt.subplots(figsize=(11, 5))
 ax.set_yscale("log")
 ax.scatter(
 primary["start_time"],
 primary["severity_usd"],
 s=70,
 color="#1f6f8b",
 zorder=3,
 label="Detected cascade (D-A, primary grid)",
 )

 for name, start, end, published in GOLDEN_EPISODES:
 start_ts, end_ts = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
 window_events = liquidations[
 (liquidations["block_timestamp"] >= start_ts)
 & (liquidations["block_timestamp"] < end_ts)
 ]
 observed_usd = float(window_events["amount_usd"].fillna(0.0).sum)
 observed_events = int(len(window_events))

 hits = primary[
 (primary["start_time"] >= start_ts) & (primary["start_time"] < end_ts)
 ]
 detected = len(hits) > 0
 grid_hits = episodes[
 (episodes["start_time"] >= start_ts)
 & (episodes["end_time"] >= start_ts)
 & (episodes["start_time"] < end_ts)
 ]
 grid_coverage = grid_hits[["w", "k", "theta_label"]].drop_duplicates.shape[0]

 summary_rows.append(
 {
 "episode": name,
 "window_start": start,
 "window_end": end,
 "published_scale": published,
 "observed_v2_usd": observed_usd,
 "observed_v2_events": observed_events,
 "detected_primary": detected,
 "grid_coverage": f"{grid_coverage}/27",
 }
 )

 mid_date = start_ts + (end_ts - start_ts) / 2
 if detected:
 row = hits.loc[hits["severity_usd"].idxmax]
 ax.annotate(
 name,
 (row["start_time"], row["severity_usd"]),
 textcoords="offset points",
 xytext=(0, 12),
 fontsize=8,
 ha="center",
 )
 else:
 y = max(observed_usd, 1.0)
 ax.scatter([mid_date], [y], marker="x", s=90, color="#888888", zorder=4)
 ax.annotate(
 f"{name}\n(not detected)",
 (mid_date, y),
 textcoords="offset points",
 xytext=(0, 12),
 fontsize=8,
 color="#555555",
 ha="center",
 )

 pd.DataFrame(summary_rows).to_csv(
 out_dir / "golden_episode_summary.csv", index=False
 )

 ax.set_xlabel("Date")
 ax.set_ylabel("Severity (USD liquidated, log scale)")
 ax.set_title(
 "CascadeSignal Atlas — Aave v2 cascades, primary D-A grid (2021–Feb 2026)"
 )
 ymin, ymax = ax.get_ylim
 ax.set_ylim(
 ymin, ymax * 6
 ) # headroom so episode-name annotations don't clip/collide
 ax.legend(loc="upper right", fontsize=9)
 ax.grid(True, which="both", axis="y", alpha=0.2)
 fig.tight_layout
 fig.savefig(out_dir / "atlas.png", dpi=150)
 plt.close(fig)


def main -> None:
 logging.basicConfig(level=logging.INFO, format="%(message)s")
 build_atlas(
 labels_path=Path("data/curated/labels/aave_v2/episodes.parquet"),
 data_dir=Path("data/raw"),
 )
 log.info(
 "Wrote atlas.png, episode_table.csv, golden_episode_summary.csv to %s", OUT_DIR
 )


if __name__ == "__main__":
 main
