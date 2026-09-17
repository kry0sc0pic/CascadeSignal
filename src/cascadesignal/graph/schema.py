"""Heterogeneous contagion-graph schema.

Defines the node/edge types and channel tags `build.py`'s snapshot builder
emits, plus the arrow schemas the resulting node/edge tables conform to when
written to parquet. Node universe and edge set follow the contagion-graph spec: nodes are
position-buckets (market x asset x HF-band) + top-~500 whale positions
individually, assets, DEX pools, and protocols; edges are collateral/debt
exposure, pool depth, and composability wraps, each tagged by contagion
`ChannelTag` so E5's channel ablation can drop one channel's edges without
touching the others.
"""

from __future__ import annotations

from enum import Enum

import numpy as np
import pandas as pd
import pyarrow as pa


class NodeType(str, Enum):
 WHALE_POSITION = "whale_position"
 POSITION_BUCKET = "position_bucket"
 ASSET = "asset"
 POOL = "pool"
 PROTOCOL = "protocol"


class ChannelTag(str, Enum):
 """Contagion transmission channel an edge belongs to."""

 COLLATERAL_EXPOSURE = "collateral_exposure"
 DEBT_EXPOSURE = "debt_exposure"
 POOL_DEPTH = "pool_depth"
 COMPOSABILITY_WRAP = "composability_wrap"


NODE_SCHEMA = pa.schema(
 [
 pa.field("node_id", pa.string), # unique within a snapshot
 pa.field("node_type", pa.string), # NodeType value
 pa.field("snapshot_block", pa.int64),
 pa.field(
 "label", pa.string
 ), # user addr / bucket key / symbol / pool addr / protocol
 pa.field("collateral_usd", pa.float64),
 pa.field("debt_usd", pa.float64),
 pa.field("health_factor", pa.float64), # NaN if no debt
 pa.field(
 "n_positions", pa.int32
 ), # 1 for whale nodes, bucket size for buckets
 ]
)

EDGE_SCHEMA = pa.schema(
 [
 pa.field("src_id", pa.string),
 pa.field("dst_id", pa.string),
 pa.field("channel", pa.string), # ChannelTag value
 pa.field("weight", pa.float64), # USD-weighted
 pa.field("snapshot_block", pa.int64),
 ]
)

# Health-factor band edges for position-bucket keys. Six bands result:
# "<=1.00", "1.00-1.10", "1.10-1.25", "1.25-1.50", "1.50-2.00", ">2.00".
HF_BAND_EDGES: tuple[float, ...] = (1.0, 1.1, 1.25, 1.5, 2.0)
NO_DEBT_BAND = "no_debt" # health_factor is NaN (position carries no debt)


def _band_labels(edges: tuple[float, ...]) -> list[str]:
 labels = [f"<={edges[0]:.2f}"]
 labels.extend(f"{lo:.2f}-{hi:.2f}" for lo, hi in zip(edges, edges[1:]))
 labels.append(f">{edges[-1]:.2f}")
 return labels


_BAND_LABELS = _band_labels(HF_BAND_EDGES)


def hf_band_series(health_factor: pd.Series) -> pd.Series:
 """Vectorized HF -> band-label assignment. NaN (no debt) -> `NO_DEBT_BAND`."""
 bins = [-np.inf, *HF_BAND_EDGES, np.inf]
 bands = pd.cut(health_factor, bins=bins, labels=_BAND_LABELS, right=True)
 return bands.astype("object").where(health_factor.notna, NO_DEBT_BAND)


# Composability wraps observed in the Aave v2 reserve set ( `reserves.py`):
# wrapped-asset symbol -> underlying-asset symbol. Only stETH is a real LSD
# wrap among the 37 reserves; aToken wraps aren't separately addressable here
# because the canonical event schema records the underlying reserve address,
# not the aToken address (see `ingest/schema.py`).
COMPOSABILITY_WRAPS: dict[str, str] = {
 "stETH": "WETH",
}


__all__ = [
 "NodeType",
 "ChannelTag",
 "NODE_SCHEMA",
 "EDGE_SCHEMA",
 "HF_BAND_EDGES",
 "NO_DEBT_BAND",
 "hf_band_series",
 "COMPOSABILITY_WRAPS",
]
