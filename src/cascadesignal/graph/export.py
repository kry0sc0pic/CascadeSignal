"""PyG/DGL-compatible export for contagion-graph snapshots.

Produces the canonical heterogeneous-graph dict both PyTorch Geometric
(`HeteroData(canonical_dict)`) and DGL (`dgl.heterograph(canonical_dict)`)
accept directly: per node-type, a local 0-based index + feature matrix; per
`(src_type, channel, dst_type)` relation, an `edge_index` array of local
indices + an `edge_weight` array. Returns plain numpy arrays, not
torch/dgl objects -- this repo doesn't depend on either library yet (that's
the next phase's model epic), so M2's training code constructs the actual
`HeteroData` / `DGLGraph` from this dict itself.

Channel ablation (E5): drop a channel's edges by filtering `edge_types` on
its `channel` key before constructing the framework graph, e.g.
`{k: v for k, v in d["edge_types"].items if k[1] != "pool_depth"}`.
"""

from __future__ import annotations

import numpy as np

from cascadesignal.graph.build import GraphSnapshot

FEATURE_COLUMNS = ("collateral_usd", "debt_usd", "health_factor", "n_positions")


def to_hetero_dict(snapshot: GraphSnapshot) -> dict:
 """Canonical heterogeneous-graph dict for one `GraphSnapshot`.

 Returns:
 {
 "block": int,
 "node_types": {node_type: {"ids": [node_id, ...], "x": (N, 4) float64 array}},
 "edge_types": {
 (src_type, channel, dst_type): {
 "edge_index": (2, E) int64 array of LOCAL indices into the
 src/dst node_type's "ids" list,
 "edge_weight": (E,) float64 array,
 }
 },
 }

 NaN feature values (e.g. `health_factor` for a no-debt or non-position
 node) are zero-filled -- GNN layers can't consume NaN directly, and zero
 reads as "not meaningful" for asset/pool/protocol nodes where the whole
 feature vector besides `n_positions` is structurally NaN.
 """
 nodes = snapshot.nodes
 node_index: dict[str, dict[str, int]] = {}
 node_types: dict[str, dict] = {}
 for ntype_key, group in nodes.groupby("node_type", sort=False):
 ntype = str(ntype_key)
 ids = group["node_id"].tolist
 node_index[ntype] = {nid: i for i, nid in enumerate(ids)}
 x = np.nan_to_num(
 group[list(FEATURE_COLUMNS)].to_numpy(dtype=np.float64), nan=0.0
 )
 node_types[ntype] = {"ids": ids, "x": x}

 type_of = nodes.set_index("node_id")["node_type"].to_dict

 edges = snapshot.edges.copy
 edge_types: dict[tuple[str, str, str], dict] = {}
 if not edges.empty:
 edges["src_type"] = edges["src_id"].map(type_of)
 edges["dst_type"] = edges["dst_id"].map(type_of)
 for group_key, group in edges.groupby(
 ["src_type", "channel", "dst_type"], sort=False
 ):
 src_type, channel, dst_type = (str(part) for part in group_key)
 src_idx = group["src_id"].map(node_index[src_type]).to_numpy(dtype=np.int64)
 dst_idx = group["dst_id"].map(node_index[dst_type]).to_numpy(dtype=np.int64)
 edge_types[(src_type, channel, dst_type)] = {
 "edge_index": np.stack([src_idx, dst_idx]),
 "edge_weight": group["weight"].to_numpy(dtype=np.float64),
 }

 return {"block": snapshot.block, "node_types": node_types, "edge_types": edge_types}


__all__ = ["to_hetero_dict", "FEATURE_COLUMNS"]
