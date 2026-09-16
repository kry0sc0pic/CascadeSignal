"""Unit tests for the contagion-graph fragility covariate (graph/fragility.py).

Synthetic nodes parquet only (no data lake): checks that the per-snapshot
`frac_at_risk` counts only `whale_position` nodes with health factor in the
danger band, over a denominator of all tracked positions (so a no-debt / NaN-HF
position counts as not-at-risk), and comes back ascending by block.
"""

from __future__ import annotations

import pandas as pd
import pytest

from cascadesignal.graph.fragility import build_fragility_covariate


def _write_nodes(path, rows: list[dict]) -> None:
    ts = pd.Timestamp("2021-01-01", tz="UTC")
    df = pd.DataFrame(rows)
    # one timestamp per block, matching the materialized layout
    df["snapshot_timestamp"] = df["snapshot_block"].map(
        lambda b: ts + pd.Timedelta(hours=int(b))
    )
    df.to_parquet(path)


def test_frac_at_risk_counts_only_danger_band_over_all_positions(tmp_path):
    path = tmp_path / "nodes.parquet"
    _write_nodes(
        path,
        [
            # block 1: 4 whale positions, 1 in [1.0, 1.25) -> 1/4
            {"node_type": "whale_position", "snapshot_block": 1, "health_factor": 1.10},
            {"node_type": "whale_position", "snapshot_block": 1, "health_factor": 2.00},
            {"node_type": "whale_position", "snapshot_block": 1, "health_factor": 0.90},
            {"node_type": "whale_position", "snapshot_block": 1, "health_factor": None},
            # asset node at the same block must be ignored entirely
            {"node_type": "asset", "snapshot_block": 1, "health_factor": 1.10},
            # block 2: 2 whale positions, both at-risk -> 2/2
            {"node_type": "whale_position", "snapshot_block": 2, "health_factor": 1.00},
            {"node_type": "whale_position", "snapshot_block": 2, "health_factor": 1.24},
        ],
    )
    cov = build_fragility_covariate(path)

    assert list(cov["snapshot_block"]) == [1, 2]  # ascending
    assert cov.set_index("snapshot_block")["frac_at_risk"][1] == pytest.approx(0.25)
    assert cov.set_index("snapshot_block")["frac_at_risk"][2] == pytest.approx(1.0)
    assert "snapshot_timestamp" in cov.columns


def test_band_bounds_are_half_open(tmp_path):
    # HF == hf_hi is excluded; HF == hf_lo is included.
    path = tmp_path / "nodes.parquet"
    _write_nodes(
        path,
        [
            {"node_type": "whale_position", "snapshot_block": 1, "health_factor": 1.25},
            {"node_type": "whale_position", "snapshot_block": 1, "health_factor": 1.00},
        ],
    )
    cov = build_fragility_covariate(path, hf_lo=1.0, hf_hi=1.25)
    assert cov["frac_at_risk"].iloc[0] == pytest.approx(0.5)
