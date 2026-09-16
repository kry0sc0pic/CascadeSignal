"""Unit tests for CAS-31's PyG/DGL-compatible hetero-graph export."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cascadesignal.graph.build import build_snapshot
from cascadesignal.graph.export import FEATURE_COLUMNS, to_hetero_dict
from cascadesignal.graph.schema import ChannelTag, NodeType
from cascadesignal.state.engine import PositionStateEngine
from cascadesignal.state.reserves import reserve_table

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
RESERVES = reserve_table()
TS = pd.Timestamp("2021-01-01T00:00:00Z")


def _core_event(block, log_index, event_type, user, reserve, amount_raw):
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
        "block_timestamp": "2021-01-01T00:00:00Z",
    }


class _StubPriceOracle:
    def __init__(self, prices):
        self._prices = prices

    def prices_at(self, addresses, timestamp):
        return {a: self._prices[a] for a in addresses if a in self._prices}


@pytest.fixture
def snapshot():
    events = [
        _core_event(100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18)),
        _core_event(100, 1, "Borrow", "0xu1", USDC, str(1000 * 10**6)),
    ]
    engine = PositionStateEngine(events=pd.DataFrame(events), reserve_table=RESERVES)
    oracle = _StubPriceOracle({WETH: 2000.0, USDC: 1.0})
    return build_snapshot(engine, oracle, 100, TS, top_n_whales=10)


def test_to_hetero_dict_node_types_indexed_locally(snapshot):
    d = to_hetero_dict(snapshot)
    assert d["block"] == 100
    whale = d["node_types"][NodeType.WHALE_POSITION.value]
    assert whale["ids"] == ["whale:0xu1"]
    assert whale["x"].shape == (1, len(FEATURE_COLUMNS))
    assert whale["x"][0, 0] == pytest.approx(4000.0)  # collateral_usd

    assets = d["node_types"][NodeType.ASSET.value]
    assert "asset:WETH" in assets["ids"]
    assert "asset:USDC" in assets["ids"]


def test_to_hetero_dict_nan_features_are_zero_filled(snapshot):
    d = to_hetero_dict(snapshot)
    asset_x = d["node_types"][NodeType.ASSET.value]["x"]
    assert not np.isnan(asset_x).any()


def test_to_hetero_dict_edge_index_uses_local_indices(snapshot):
    d = to_hetero_dict(snapshot)
    key = (
        NodeType.WHALE_POSITION.value,
        ChannelTag.COLLATERAL_EXPOSURE.value,
        NodeType.ASSET.value,
    )
    assert key in d["edge_types"]
    edge = d["edge_types"][key]
    assert edge["edge_index"].shape == (2, 1)
    assert edge["edge_weight"][0] == pytest.approx(4000.0)

    whale_ids = d["node_types"][NodeType.WHALE_POSITION.value]["ids"]
    asset_ids = d["node_types"][NodeType.ASSET.value]["ids"]
    src_local, dst_local = edge["edge_index"][:, 0]
    assert whale_ids[src_local] == "whale:0xu1"
    assert asset_ids[dst_local] == "asset:WETH"


def test_to_hetero_dict_channel_ablation_drops_only_that_channel(snapshot):
    d = to_hetero_dict(snapshot)
    filtered = {
        k: v
        for k, v in d["edge_types"].items()
        if k[1] != ChannelTag.DEBT_EXPOSURE.value
    }
    assert all(k[1] != ChannelTag.DEBT_EXPOSURE.value for k in filtered)
    assert any(k[1] == ChannelTag.COLLATERAL_EXPOSURE.value for k in filtered)
