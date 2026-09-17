"""Contagion-graph network visualization.

Renders real materialized contagion-graph snapshots
(`scripts/graph/materialize_snapshots.py`'s output, `data/curated/graph/aave_v2/`)
as node-link diagrams: one panel from inside the Terra/LUNA
cascade window (D-A-detected, `docs/episodes/terra-2022.md`), one from the FTX
week (D-A non-detected) as an honest contrast panel.

Snapshot selection:
 - Terra: the materialized cadence over this window is the 7200-block "normal"
 cadence (no stress-window densification triggered here -- see
 `graph.stress_windows`), so there is no snapshot exactly inside the
 primary D-A episode's 162-block window. This picks the snapshot nearest in
 time to that episode's start (read from `experiments/E1/output/episode_table.csv`,
 the `is_primary` row inside the Terra window), which lands ~14h before it.
 - FTX: no D-A-detected episode exists in this window (0/27 grid coverage,
 `experiments/E1/output/golden_episode_summary.csv`) to anchor on, so the
 busiest (most-edges) snapshot in the window stands in as a reference point.

Rendering: the full snapshot (~700 nodes / ~4,700 edges, dominated by the
long-tail `position_bucket` aggregate) is an unreadable hairball, so both
panels keep only the top-N `whale_position` nodes by `collateral_usd` plus any
`asset`/`pool`/`protocol` node they connect to -- `position_bucket` nodes and
disconnected structural nodes (e.g. the `protocol` node, which this schema
never wires to an edge) are dropped. Node size ~ collateral_usd (sqrt scale),
node color by node_type, edge color+style by channel, per the dataviz skill's
validated categorical palette (`palette.md`).

Output (reproducible, not hand-pasted):
 - paper/figures/graph_network_terra.png
 - paper/figures/graph_network_ftx.png

Run (from repo root):
 python scripts/graph/render_network.py
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D

from cascadesignal.graph.build import GraphSnapshot
from cascadesignal.graph.io import load_snapshots
from cascadesignal.graph.schema import ChannelTag, NodeType

log = logging.getLogger(__name__)

SNAPSHOT_DIR = Path("data/curated/graph/aave_v2")
EPISODE_TABLE = Path("experiments/E1/output/episode_table.csv")
GOLDEN_SUMMARY = Path("experiments/E1/output/golden_episode_summary.csv")
OUT_DIR = Path("paper/figures")

DISPLAY_TOP_N_WHALES = 40

TERRA_WINDOW = (
 pd.Timestamp("2022-05-01", tz="UTC"),
 pd.Timestamp("2022-06-20", tz="UTC"),
)
FTX_WINDOW = (
 pd.Timestamp("2022-11-06", tz="UTC"),
 pd.Timestamp("2022-11-11", tz="UTC"),
)

STRUCTURAL_TYPES = (NodeType.ASSET.value, NodeType.POOL.value, NodeType.PROTOCOL.value)

# dataviz skill categorical palette (references/palette.md), validated via
# scripts/validate_palette.js --mode light: nodes take slots 1-4, edge
# channels take slots 5-8, so the two legends never share a hue.
NODE_COLORS = {
 NodeType.WHALE_POSITION.value: "#2a78d6", # slot 1 blue
 NodeType.ASSET.value: "#1baf7a", # slot 2 aqua
 NodeType.POOL.value: "#eda100", # slot 3 yellow
 NodeType.PROTOCOL.value: "#008300", # slot 4 green
}
NODE_ORDER = [
 NodeType.WHALE_POSITION.value,
 NodeType.ASSET.value,
 NodeType.POOL.value,
 NodeType.PROTOCOL.value,
]
NODE_LABELS = {
 NodeType.WHALE_POSITION.value: "whale position (top 500 by collateral)",
 NodeType.ASSET.value: "asset",
 NodeType.POOL.value: "DEX pool",
 NodeType.PROTOCOL.value: "protocol",
}

EDGE_COLORS = {
 ChannelTag.COLLATERAL_EXPOSURE.value: "#4a3aa7", # slot 5 violet
 ChannelTag.DEBT_EXPOSURE.value: "#e34948", # slot 6 red
 ChannelTag.POOL_DEPTH.value: "#e87ba4", # slot 7 magenta
 ChannelTag.COMPOSABILITY_WRAP.value: "#eb6834", # slot 8 orange
}
LineStyle = Literal["solid", "dashed", "dashdot", "dotted"]
EDGE_STYLES: dict[str, LineStyle] = {
 ChannelTag.COLLATERAL_EXPOSURE.value: "solid",
 ChannelTag.DEBT_EXPOSURE.value: "dashed",
 ChannelTag.POOL_DEPTH.value: "dashdot",
 ChannelTag.COMPOSABILITY_WRAP.value: "dotted",
}
EDGE_ORDER = [
 ChannelTag.COLLATERAL_EXPOSURE.value,
 ChannelTag.DEBT_EXPOSURE.value,
 ChannelTag.POOL_DEPTH.value,
 ChannelTag.COMPOSABILITY_WRAP.value,
]
EDGE_LABELS = {
 ChannelTag.COLLATERAL_EXPOSURE.value: "collateral exposure",
 ChannelTag.DEBT_EXPOSURE.value: "debt exposure",
 ChannelTag.POOL_DEPTH.value: "pool depth",
 ChannelTag.COMPOSABILITY_WRAP.value: "composability wrap",
}

INK = "#0b0b0b"
MUTED = "#52514e"
SURFACE = "#fcfcfb"
WHALE_MARKER_RANGE = (35, 500)
STRUCTURAL_MARKER_SIZE = 280


def _episode_table -> pd.DataFrame:
 table = pd.read_csv(EPISODE_TABLE)
 table["start_time"] = pd.to_datetime(table["start_time"], utc=True)
 table["end_time"] = pd.to_datetime(table["end_time"], utc=True)
 return table


def terra_primary_episode -> pd.Series:
 """The single D-A primary-grid episode inside the Terra window (June 14 stETH-depeg/Celsius sub-episode)."""
 table = _episode_table
 in_window = table["start_time"].between(*TERRA_WINDOW)
 primary = table[in_window & table["is_primary"]]
 if len(primary) != 1:
 raise RuntimeError(
 f"expected exactly one primary Terra-window episode, found {len(primary)}"
 )
 return primary.iloc[0]


def golden_summary_row(episode: str) -> pd.Series:
 summary = pd.read_csv(GOLDEN_SUMMARY)
 row = summary[summary["episode"] == episode]
 if len(row) != 1:
 raise RuntimeError(
 f"expected exactly one golden-summary row for {episode!r}, found {len(row)}"
 )
 return row.iloc[0]


def pick_nearest(snaps: list[GraphSnapshot], target_ts: pd.Timestamp) -> GraphSnapshot:
 return min(snaps, key=lambda s: abs((s.timestamp - target_ts).total_seconds))


def pick_busiest(snaps: list[GraphSnapshot]) -> GraphSnapshot:
 return max(snaps, key=lambda s: len(s.edges))


def snapshots_in_window(
 snaps: list[GraphSnapshot], window: tuple[pd.Timestamp, pd.Timestamp]
) -> list[GraphSnapshot]:
 start, end = window
 return [s for s in snaps if start <= s.timestamp <= end]


def filter_for_legibility(
 snap: GraphSnapshot, top_n: int = DISPLAY_TOP_N_WHALES
) -> tuple[pd.DataFrame, pd.DataFrame]:
 """Keep the top-N whales by collateral_usd plus any structural node they connect to.

 Drops position_bucket nodes (the long-tail aggregate) and any structural
 node (asset/pool/protocol) left disconnected once non-top whales and
 position_bucket edges are gone -- e.g. the protocol node, which this
 schema never wires to an edge.
 """
 nodes, edges = snap.nodes, snap.edges
 whales = nodes[nodes["node_type"] == NodeType.WHALE_POSITION.value].sort_values(
 "collateral_usd", ascending=False
 )
 top_whale_ids = set(whales.head(top_n)["node_id"])
 structural_ids = set(nodes[nodes["node_type"].isin(STRUCTURAL_TYPES)]["node_id"])

 candidate_ids = top_whale_ids | structural_ids
 sub_edges = edges[
 edges["src_id"].isin(candidate_ids) & edges["dst_id"].isin(candidate_ids)
 ]
 connected_structural = (
 set(sub_edges["src_id"]) | set(sub_edges["dst_id"])
 ) & structural_ids

 keep_ids = top_whale_ids | connected_structural
 sub_nodes = nodes[nodes["node_id"].isin(keep_ids)].copy
 sub_edges = sub_edges[
 sub_edges["src_id"].isin(keep_ids) & sub_edges["dst_id"].isin(keep_ids)
 ]
 return sub_nodes, sub_edges


def _node_sizes(nodes: pd.DataFrame) -> pd.Series:
 sizes = pd.Series(STRUCTURAL_MARKER_SIZE, index=nodes.index, dtype=float)
 is_whale = nodes["node_type"] == NodeType.WHALE_POSITION.value
 sqrt_c = np.sqrt(nodes.loc[is_whale, "collateral_usd"])
 lo, hi = sqrt_c.min, sqrt_c.max
 lo_size, hi_size = WHALE_MARKER_RANGE
 if hi > lo:
 sizes.loc[is_whale] = lo_size + (hi_size - lo_size) * (sqrt_c - lo) / (hi - lo)
 else:
 sizes.loc[is_whale] = lo_size
 return sizes


def _build_layout_graph(nodes: pd.DataFrame, edges: pd.DataFrame) -> nx.Graph:
 g: nx.Graph = nx.Graph
 g.add_nodes_from(nodes["node_id"])
 g.add_edges_from(zip(edges["src_id"], edges["dst_id"]))
 return g


def _bipartite_positions(
 nodes: pd.DataFrame, edges: pd.DataFrame
) -> dict[str, tuple[float, float]]:
 """Whales in a left column (sorted by collateral_usd), structural nodes in a right
 column (sorted by degree) -- this graph is near-bipartite (whales only ever connect
 to asset/pool nodes, never to each other), so a force-directed layout collapses every
 whale onto its shared assets into one unreadable central clump. Two ranked columns
 keep every node on its own row -- no overlap possible -- and the column ordering
 itself is informative (biggest whale / most-shared asset nearest the top).
 """
 whales = nodes[nodes["node_type"] == NodeType.WHALE_POSITION.value].sort_values(
 "collateral_usd", ascending=False
 )
 structural = nodes[nodes["node_type"] != NodeType.WHALE_POSITION.value]
 degree = pd.concat([edges["src_id"], edges["dst_id"]]).value_counts
 structural = structural.assign(_degree=structural["node_id"].map(degree).fillna(0))
 structural = structural.sort_values("_degree", ascending=False)

 pos: dict[str, tuple[float, float]] = {}
 n_w = len(whales)
 for i, node_id in enumerate(whales["node_id"]):
 pos[node_id] = (-1.0, 1.0 - (2.0 * i / max(n_w - 1, 1)))
 n_s = len(structural)
 for i, node_id in enumerate(structural["node_id"]):
 pos[node_id] = (1.0, 1.0 - (2.0 * i / max(n_s - 1, 1)))
 return pos


def render_panel(
 ax: plt.Axes,
 nodes: pd.DataFrame,
 edges: pd.DataFrame,
 title: str,
 caption: str,
) -> None:
 layout_graph = _build_layout_graph(nodes, edges)
 pos = _bipartite_positions(nodes, edges)

 sizes = _node_sizes(nodes)
 for node_type in NODE_ORDER:
 mask = nodes["node_type"] == node_type
 if not mask.any:
 continue
 ids = nodes.loc[mask, "node_id"]
 xy = np.array([pos[i] for i in ids])
 ax.scatter(
 xy[:, 0],
 xy[:, 1],
 s=sizes.loc[mask.index[mask]],
 c=NODE_COLORS[node_type],
 edgecolors="white",
 linewidths=0.4,
 zorder=3,
 label=None,
 )

 for channel in EDGE_ORDER:
 sub = edges[edges["channel"] == channel]
 if sub.empty:
 continue
 nx.draw_networkx_edges(
 layout_graph,
 pos,
 ax=ax,
 edgelist=list(zip(sub["src_id"], sub["dst_id"])),
 edge_color=EDGE_COLORS[channel],
 style=EDGE_STYLES[channel],
 width=0.8,
 alpha=0.4,
 )

 asset_labels = {
 row["node_id"]: row["label"]
 for _, row in nodes[nodes["node_type"] == NodeType.ASSET.value].iterrows
 }
 for node_id, label in asset_labels.items:
 x, y = pos[node_id]
 ax.text(
 x + 0.05,
 y,
 label,
 fontsize=7.5,
 color=INK,
 ha="left",
 va="center",
 zorder=4,
 )

 ax.set_xlim(-1.35, 1.9)
 ax.set_ylim(-1.12, 1.12)
 ax.set_title(title, fontsize=12, color=INK, loc="left", fontweight="bold")
 ax.text(
 0.0,
 1.06,
 caption,
 transform=ax.transAxes,
 fontsize=8.5,
 color=MUTED,
 va="bottom",
 wrap=True,
 )
 ax.set_facecolor(SURFACE)
 ax.set_xticks([])
 ax.set_yticks([])
 for spine in ax.spines.values:
 spine.set_visible(False)


def _legend_handles(present_node_types: set[str]) -> tuple[list[Line2D], list[Line2D]]:
 node_handles = [
 Line2D(
 [0],
 [0],
 marker="o",
 linestyle="None",
 markerfacecolor=NODE_COLORS[t],
 markeredgecolor="white",
 markersize=9,
 label=NODE_LABELS[t],
 )
 for t in NODE_ORDER
 if t in present_node_types
 ]
 edge_handles = [
 Line2D(
 [0],
 [0],
 color=EDGE_COLORS[c],
 linestyle=EDGE_STYLES[c],
 linewidth=1.6,
 label=EDGE_LABELS[c],
 )
 for c in EDGE_ORDER
 ]
 return node_handles, edge_handles


def build_terra_panel(
 snaps: list[GraphSnapshot],
) -> tuple[pd.DataFrame, pd.DataFrame, str, str]:
 episode = terra_primary_episode
 window_snaps = snapshots_in_window(snaps, TERRA_WINDOW)
 snap = pick_nearest(window_snaps, episode["start_time"])
 sub_nodes, sub_edges = filter_for_legibility(snap)

 title = "Terra/LUNA cascade window (D-A detected)"
 caption = (
 f"Block {snap.block:,} · {snap.timestamp:%Y-%m-%d %H:%M} UTC — closest materialized "
 f"snapshot to the primary D-A episode ({episode['start_time']:%Y-%m-%d %H:%M} UTC, "
 f"${episode['total_liquidated_usd']/1e6:.1f}M / {episode['num_positions']} positions / "
 f"{episode['max_generations']} generations). Top {DISPLAY_TOP_N_WHALES} whales by collateral_usd "
 f"+ connected asset/pool nodes shown ({len(sub_nodes)} of {len(snap.nodes)} nodes, "
 f"{len(sub_edges)} of {len(snap.edges)} edges); position_bucket long tail omitted."
 )
 return sub_nodes, sub_edges, title, caption


def build_ftx_panel(
 snaps: list[GraphSnapshot],
) -> tuple[pd.DataFrame, pd.DataFrame, str, str]:
 summary = golden_summary_row("FTX collapse")
 window_snaps = snapshots_in_window(snaps, FTX_WINDOW)
 snap = pick_busiest(window_snaps)
 sub_nodes, sub_edges = filter_for_legibility(snap)

 title = "FTX week (D-A non-detected — contrast panel)"
 caption = (
 f"Block {snap.block:,} · {snap.timestamp:%Y-%m-%d %H:%M} UTC — busiest materialized "
 f"snapshot in the window (no primary D-A episode to anchor on: {summary['grid_coverage']} grid "
 f"coverage, ${summary['observed_v2_usd']/1e6:.1f}M / {summary['observed_v2_events']} liquidation "
 f"events observed). Top {DISPLAY_TOP_N_WHALES} whales by collateral_usd + connected asset/pool "
 f"nodes shown ({len(sub_nodes)} of {len(snap.nodes)} nodes, {len(sub_edges)} of {len(snap.edges)} "
 f"edges); position_bucket long tail omitted."
 )
 return sub_nodes, sub_edges, title, caption


def _save_panel(build_fn, snaps: list[GraphSnapshot], out_path: Path) -> None:
 nodes, edges, title, caption = build_fn(snaps)
 fig, ax = plt.subplots(figsize=(13, 14))
 fig.patch.set_facecolor(SURFACE)
 render_panel(ax, nodes, edges, title, caption)

 node_handles, edge_handles = _legend_handles(set(nodes["node_type"].unique))
 fig.legend(
 handles=node_handles,
 loc="lower left",
 bbox_to_anchor=(0.06, 0.01),
 fontsize=8.5,
 title="Node type",
 frameon=False,
 )
 fig.legend(
 handles=edge_handles,
 loc="lower right",
 bbox_to_anchor=(0.97, 0.01),
 fontsize=8.5,
 title="Edge channel",
 frameon=False,
 )

 fig.tight_layout(rect=(0, 0.07, 1, 0.94))
 OUT_DIR.mkdir(parents=True, exist_ok=True)
 fig.savefig(out_path, dpi=150, facecolor=SURFACE)
 plt.close(fig)
 log.info("wrote %s", out_path)


def main -> None:
 logging.basicConfig(level=logging.INFO, format="%(message)s")
 snaps = load_snapshots(SNAPSHOT_DIR)
 _save_panel(build_terra_panel, snaps, OUT_DIR / "graph_network_terra.png")
 _save_panel(build_ftx_panel, snaps, OUT_DIR / "graph_network_ftx.png")


if __name__ == "__main__":
 main
