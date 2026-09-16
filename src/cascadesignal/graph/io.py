"""Persistence for materialized contagion-graph snapshots (CAS-31 / MVP-13).

`graph.build.build_snapshot(s)` returns in-memory `GraphSnapshot` objects.
This module is the file-format seam that lets the full-period materialization
job (`scripts/graph/materialize_snapshots.py`) write its output once and have
a downstream consumer load it back without re-running state reconstruction
over the whole study period.

Two flat parquet files -- `nodes.parquet` / `edges.parquet` -- hold every
snapshot concatenated, conforming to `graph.schema.NODE_SCHEMA`/`EDGE_SCHEMA`
plus two denormalized per-snapshot columns (`snapshot_timestamp`, `protocol`)
so each snapshot's own metadata round-trips without a separate index file to
join against.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from cascadesignal.graph.build import EDGE_COLUMNS, GraphSnapshot, NODE_FEATURE_COLUMNS
from cascadesignal.graph.schema import EDGE_SCHEMA, NODE_SCHEMA

NODES_FILENAME = "nodes.parquet"
EDGES_FILENAME = "edges.parquet"

_META_FIELDS = [
    pa.field("snapshot_timestamp", pa.timestamp("us", tz="UTC")),
    pa.field("protocol", pa.string()),
]
_FILE_NODE_SCHEMA = pa.schema([*NODE_SCHEMA, *_META_FIELDS])
_FILE_EDGE_SCHEMA = pa.schema([*EDGE_SCHEMA, *_META_FIELDS])

_NODE_DTYPES = {
    "node_id": "string",
    "node_type": "string",
    "label": "string",
    "collateral_usd": "float64",
    "debt_usd": "float64",
    "health_factor": "float64",
    "n_positions": "int32",
    "snapshot_block": "int64",
}
_EDGE_DTYPES = {
    "src_id": "string",
    "dst_id": "string",
    "channel": "string",
    "weight": "float64",
    "snapshot_block": "int64",
}


def save_snapshots(snapshots: list[GraphSnapshot], out_dir: str | Path) -> None:
    """Write every snapshot's nodes/edges to `out_dir`.

    Full-materialization artifact, not incremental -- overwrites any existing
    files at `out_dir`. Raises on an empty `snapshots` list rather than
    silently writing empty files, since that's never a legitimate production
    output.
    """
    if not snapshots:
        raise ValueError("snapshots is empty -- nothing to save")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    node_parts = []
    edge_parts = []
    for snap in snapshots:
        nodes = snap.nodes.assign(
            snapshot_timestamp=snap.timestamp, protocol=snap.protocol
        )
        edges = snap.edges.assign(
            snapshot_timestamp=snap.timestamp, protocol=snap.protocol
        )
        node_parts.append(
            nodes[
                [
                    *NODE_FEATURE_COLUMNS,
                    "snapshot_block",
                    "snapshot_timestamp",
                    "protocol",
                ]
            ]
        )
        edge_parts.append(
            edges[[*EDGE_COLUMNS, "snapshot_block", "snapshot_timestamp", "protocol"]]
        )

    all_nodes = pd.concat(node_parts, ignore_index=True).astype(_NODE_DTYPES)
    all_edges = pd.concat(edge_parts, ignore_index=True).astype(_EDGE_DTYPES)

    pq.write_table(
        pa.Table.from_pandas(all_nodes, schema=_FILE_NODE_SCHEMA, preserve_index=False),
        out_dir / NODES_FILENAME,
    )
    pq.write_table(
        pa.Table.from_pandas(all_edges, schema=_FILE_EDGE_SCHEMA, preserve_index=False),
        out_dir / EDGES_FILENAME,
    )


def load_snapshots(data_dir: str | Path) -> list[GraphSnapshot]:
    """Reconstitute the `list[GraphSnapshot]` `save_snapshots` wrote.

    Returned in ascending block order. Inverse of `save_snapshots`: each
    snapshot's `.nodes`/`.edges` frames come back with exactly the columns
    `graph.build.build_snapshot` originally produced (the denormalized
    `snapshot_timestamp`/`protocol` columns are consumed into `.timestamp`/
    `.protocol`, not left on the frames).
    """
    data_dir = Path(data_dir)
    nodes = pd.read_parquet(data_dir / NODES_FILENAME)
    edges = pd.read_parquet(data_dir / EDGES_FILENAME)

    edges_by_block = dict(tuple(edges.groupby("snapshot_block", sort=False)))
    empty_edges = edges.iloc[0:0]

    snapshots: list[GraphSnapshot] = []
    for block, node_group in nodes.groupby("snapshot_block", sort=True):
        edge_group = edges_by_block.get(block, empty_edges)
        snapshots.append(
            GraphSnapshot(
                block=int(block),  # type: ignore[arg-type]
                timestamp=pd.Timestamp(node_group["snapshot_timestamp"].iloc[0]),
                protocol=str(node_group["protocol"].iloc[0]),
                nodes=node_group[[*NODE_FEATURE_COLUMNS, "snapshot_block"]].reset_index(
                    drop=True
                ),
                edges=edge_group[[*EDGE_COLUMNS, "snapshot_block"]].reset_index(
                    drop=True
                ),
            )
        )
    return snapshots


__all__ = ["save_snapshots", "load_snapshots", "NODES_FILENAME", "EDGES_FILENAME"]
