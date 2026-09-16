"""Cascade episode label schema (CAS-16 / ADR-001).

Output partition layout:
  data/curated/labels/{protocol_tag}/w{w}_k{k}_theta{theta_label}.parquet
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa

CASCADE_LABEL_SCHEMA = pa.schema(
    [
        pa.field("episode_id", pa.string()),
        pa.field("protocol_tag", pa.string()),
        pa.field("definition", pa.string()),  # "D-A" per ADR-001
        pa.field("w", pa.int32()),
        pa.field("k", pa.int32()),
        pa.field("theta_label", pa.string()),
        pa.field("theta_usd", pa.float64()),
        pa.field("generation_lag_blocks", pa.int32()),
        pa.field("is_primary", pa.bool_()),
        pa.field("start_block", pa.int64()),
        pa.field("end_block", pa.int64()),
        pa.field("start_time", pa.timestamp("us", tz="UTC")),
        pa.field("end_time", pa.timestamp("us", tz="UTC")),
        pa.field("total_liquidated_usd", pa.float64()),
        pa.field("num_positions", pa.int32()),
        pa.field("num_accounts", pa.int32()),
        pa.field("max_generations", pa.int32()),
        # Mock 1 severity proxy = total_liquidated_usd. ADR-002 (CAS-29) built
        # a fuller severity_usd_total (+ bad debt), but that alt-labeler/
        # severity track is out of scope for the current Hawkes-EWS cut, so
        # this field stays the proxy.
        pa.field("severity_usd", pa.float64()),
    ]
)


def to_arrow(df: pd.DataFrame) -> pa.Table:
    """Convert a labeled episodes DataFrame to a PyArrow table with the canonical schema."""
    ordered = df[[f.name for f in CASCADE_LABEL_SCHEMA]]
    return pa.Table.from_pandas(
        ordered, schema=CASCADE_LABEL_SCHEMA, safe=False, preserve_index=False
    )
