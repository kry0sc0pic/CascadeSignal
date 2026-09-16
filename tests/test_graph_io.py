"""Unit tests for contagion-graph snapshot persistence (CAS-31 / MVP-13).

Round-trips real `build_snapshot` output through `save_snapshots`/
`load_snapshots` -- reuses `test_graph_build.py`'s synthetic fixtures rather
than hand-rolling `GraphSnapshot` instances, so the tested schema is exactly
what the builder actually produces.
"""

from __future__ import annotations

import pandas as pd
import pytest

from cascadesignal.graph.build import build_snapshot
from cascadesignal.graph.io import load_snapshots, save_snapshots
from cascadesignal.state.engine import PositionStateEngine
from cascadesignal.state.reserves import reserve_table

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
RESERVES = reserve_table()


def _core_event(block, log_index, event_type, user, reserve, amount_raw, ts):
    return {
        "chain_id": 1,
        "block_number": block,
        "log_index": log_index,
        "tx_hash": f"0xtx{block}_{log_index}",
        "protocol": "aave_v2",
        "event_type": event_type,
        "user": user,
        "collateral_asset": None,
        "debt_asset": reserve,
        "amount_raw": amount_raw,
        "amount_usd": None,
        "liquidator": None,
        "collateral_seized_raw": None,
        "collateral_seized_usd": None,
        "block_timestamp": ts,
    }


class _StubPriceOracle:
    def __init__(self, prices: dict[str, float]):
        self._prices = prices

    def prices_at(self, addresses, timestamp, block_number=None, log_index=None):
        return {a: self._prices[a] for a in addresses if a in self._prices}


def _two_real_snapshots() -> list:
    events = [
        _core_event(
            100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18), "2021-01-01T00:00:00Z"
        ),
        _core_event(
            100, 1, "Borrow", "0xu1", USDC, str(1000 * 10**6), "2021-01-01T00:00:00Z"
        ),
        _core_event(
            200, 0, "Deposit", "0xu2", USDC, str(5000 * 10**6), "2021-01-02T00:00:00Z"
        ),
    ]
    engine = PositionStateEngine(events=pd.DataFrame(events), reserve_table=RESERVES)
    oracle = _StubPriceOracle({WETH: 2000.0, USDC: 1.0})
    snap1 = build_snapshot(
        engine, oracle, 100, pd.Timestamp("2021-01-01T00:00:00Z"), top_n_whales=10
    )
    snap2 = build_snapshot(
        engine, oracle, 200, pd.Timestamp("2021-01-02T00:00:00Z"), top_n_whales=10
    )
    return [snap1, snap2]


def test_round_trip_preserves_blocks_timestamps_protocol(tmp_path):
    snapshots = _two_real_snapshots()
    save_snapshots(snapshots, tmp_path)
    loaded = load_snapshots(tmp_path)

    assert [s.block for s in loaded] == [100, 200]
    assert [s.protocol for s in loaded] == ["aave_v2", "aave_v2"]
    assert loaded[0].timestamp == pd.Timestamp("2021-01-01T00:00:00Z")
    assert loaded[1].timestamp == pd.Timestamp("2021-01-02T00:00:00Z")


def test_round_trip_preserves_node_and_edge_content(tmp_path):
    snapshots = _two_real_snapshots()
    save_snapshots(snapshots, tmp_path)
    loaded = load_snapshots(tmp_path)

    for original, restored in zip(snapshots, loaded):
        orig_nodes = original.nodes.sort_values("node_id").reset_index(drop=True)
        rest_nodes = restored.nodes.sort_values("node_id").reset_index(drop=True)
        pd.testing.assert_frame_equal(
            orig_nodes, rest_nodes[orig_nodes.columns], check_dtype=False
        )

        orig_edges = original.edges.sort_values(
            ["src_id", "dst_id", "channel"]
        ).reset_index(drop=True)
        rest_edges = restored.edges.sort_values(
            ["src_id", "dst_id", "channel"]
        ).reset_index(drop=True)
        pd.testing.assert_frame_equal(
            orig_edges, rest_edges[orig_edges.columns], check_dtype=False
        )


def test_load_returns_ascending_block_order_regardless_of_save_order(tmp_path):
    snap1, snap2 = _two_real_snapshots()
    save_snapshots([snap2, snap1], tmp_path)
    loaded = load_snapshots(tmp_path)
    assert [s.block for s in loaded] == [100, 200]


def test_save_snapshots_rejects_empty_list(tmp_path):
    with pytest.raises(ValueError):
        save_snapshots([], tmp_path)
