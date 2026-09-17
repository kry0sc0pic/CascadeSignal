"""Unit tests for the contagion-graph snapshot builder.

Synthetic engine/oracle fixtures, mirroring tests/test_position_snapshots.py's
style -- no real ingested data required.
"""

from __future__ import annotations

import pandas as pd
import pytest

import cascadesignal.graph.build as build_mod
from cascadesignal.graph.build import build_snapshot, build_snapshots
from cascadesignal.graph.schema import ChannelTag, NodeType
from cascadesignal.state.engine import PositionStateEngine
from cascadesignal.state.reserves import reserve_table

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
STETH = "0xae7ab96520de3a18e5e111b5eaab095312d7fe84"
RESERVES = reserve_table
TS = pd.Timestamp("2021-01-01T00:00:00Z")


def _core_event(
 block, log_index, event_type, user, reserve, amount_raw, ts="2021-01-01T00:00:00Z"
):
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

 def prices_at(self, addresses, timestamp):
 return {a: self._prices[a] for a in addresses if a in self._prices}


def _engine(events):
 return PositionStateEngine(events=pd.DataFrame(events), reserve_table=RESERVES)


def _pool_bars(pool_address, symbol, rows):
 return pd.DataFrame(
 {
 "dex": "uniswap_v2",
 "pool_address": pool_address,
 "collateral_symbol": symbol,
 "date": pd.to_datetime([r[0] for r in rows], utc=True),
 "depth_usd": [r[1] for r in rows],
 }
 )


def test_single_whale_gets_node_and_exposure_edges:
 events = [
 _core_event(100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18)),
 _core_event(100, 1, "Borrow", "0xu1", USDC, str(1000 * 10**6)),
 ]
 engine = _engine(events)
 oracle = _StubPriceOracle({WETH: 2000.0, USDC: 1.0})

 snap = build_snapshot(engine, oracle, 100, TS, top_n_whales=10)
 nodes = snap.nodes.set_index("node_id")
 assert nodes.loc["whale:0xu1", "node_type"] == NodeType.WHALE_POSITION.value
 assert nodes.loc["whale:0xu1", "collateral_usd"] == pytest.approx(4000.0)
 assert nodes.loc["whale:0xu1", "debt_usd"] == pytest.approx(1000.0)
 assert "asset:WETH" in nodes.index
 assert "asset:USDC" in nodes.index
 assert "protocol:aave_v2" in nodes.index

 edges = snap.edges
 coll = edges[edges["channel"] == ChannelTag.COLLATERAL_EXPOSURE.value]
 assert coll.iloc[0]["src_id"] == "whale:0xu1"
 assert coll.iloc[0]["dst_id"] == "asset:WETH"
 assert coll.iloc[0]["weight"] == pytest.approx(4000.0)

 debt = edges[edges["channel"] == ChannelTag.DEBT_EXPOSURE.value]
 assert debt.iloc[0]["src_id"] == "whale:0xu1"
 assert debt.iloc[0]["dst_id"] == "asset:USDC"
 assert debt.iloc[0]["weight"] == pytest.approx(1000.0)


def test_non_whale_positions_bucketed_by_market_asset_hf_band:
 # Two users, same dominant asset (WETH), different HF bands (no debt vs.
 # heavily borrowed) -- must land in two distinct bucket nodes.
 events = [
 _core_event(100, 0, "Deposit", "0xnodebt", WETH, str(1 * 10**18)),
 _core_event(100, 1, "Deposit", "0xleveraged", WETH, str(1 * 10**18)),
 _core_event(100, 2, "Borrow", "0xleveraged", USDC, str(1500 * 10**6)),
 ]
 engine = _engine(events)
 oracle = _StubPriceOracle({WETH: 2000.0, USDC: 1.0})

 # top_n_whales=0 forces every position into a bucket.
 snap = build_snapshot(engine, oracle, 100, TS, top_n_whales=0)
 buckets = snap.nodes[snap.nodes["node_type"] == NodeType.POSITION_BUCKET.value]
 assert len(buckets) == 2
 ids = set(buckets["node_id"])
 assert any(nid.startswith("bucket:aave_v2:WETH:no_debt") for nid in ids)
 # weighted_collateral=2000*0.86=1720, debt=1500 -> HF ~1.1467 -> band "1.10-1.25"
 assert any(nid.startswith("bucket:aave_v2:WETH:1.10-1.25") for nid in ids)

 no_debt = buckets[buckets["node_id"].str.contains("no_debt")].iloc[0]
 assert no_debt["n_positions"] == 1
 assert no_debt["collateral_usd"] == pytest.approx(2000.0)


def test_two_users_same_bucket_are_aggregated:
 events = [
 _core_event(100, 0, "Deposit", "0xaaa", WETH, str(1 * 10**18)),
 _core_event(100, 1, "Deposit", "0xbbb", WETH, str(1 * 10**18)),
 ]
 engine = _engine(events)
 oracle = _StubPriceOracle({WETH: 2000.0})

 snap = build_snapshot(engine, oracle, 100, TS, top_n_whales=0)
 buckets = snap.nodes[snap.nodes["node_type"] == NodeType.POSITION_BUCKET.value]
 assert len(buckets) == 1
 row = buckets.iloc[0]
 assert row["n_positions"] == 2
 assert row["collateral_usd"] == pytest.approx(4000.0)

 coll_edges = snap.edges[
 snap.edges["channel"] == ChannelTag.COLLATERAL_EXPOSURE.value
 ]
 assert len(coll_edges) == 1
 assert coll_edges.iloc[0]["weight"] == pytest.approx(4000.0)


def test_whale_selection_ties_broken_deterministically_by_address:
 events = [
 _core_event(100, 0, "Deposit", "0xbbb", WETH, str(1 * 10**18)),
 _core_event(100, 1, "Deposit", "0xaaa", WETH, str(1 * 10**18)),
 ]
 engine = _engine(events)
 oracle = _StubPriceOracle({WETH: 2000.0})

 snap = build_snapshot(engine, oracle, 100, TS, top_n_whales=1)
 whales = snap.nodes[snap.nodes["node_type"] == NodeType.WHALE_POSITION.value]
 assert list(whales["node_id"]) == ["whale:0xaaa"]


def test_node_cap_guard_raises_when_exceeded(monkeypatch):
 events = [_core_event(100, 0, "Deposit", "0xu1", WETH, str(1 * 10**18))]
 engine = _engine(events)
 oracle = _StubPriceOracle({WETH: 2000.0})

 monkeypatch.setattr(build_mod, "MAX_NODES_PER_SNAPSHOT", 1)
 with pytest.raises(ValueError):
 build_snapshot(engine, oracle, 100, TS, top_n_whales=10)


def test_composability_wrap_edge_reflects_steth_collateral:
 events = [_core_event(100, 0, "Deposit", "0xu1", STETH, str(1 * 10**18))]
 engine = _engine(events)
 oracle = _StubPriceOracle({STETH: 1900.0})

 snap = build_snapshot(engine, oracle, 100, TS, top_n_whales=10)
 wrap = snap.edges[snap.edges["channel"] == ChannelTag.COMPOSABILITY_WRAP.value]
 assert len(wrap) == 1
 assert wrap.iloc[0]["src_id"] == "asset:stETH"
 assert wrap.iloc[0]["dst_id"] == "asset:WETH"
 assert wrap.iloc[0]["weight"] == pytest.approx(1900.0)


def test_composability_wrap_edge_present_even_with_zero_steth:
 events = [_core_event(100, 0, "Deposit", "0xu1", WETH, str(1 * 10**18))]
 engine = _engine(events)
 oracle = _StubPriceOracle({WETH: 2000.0})

 snap = build_snapshot(engine, oracle, 100, TS, top_n_whales=10)
 wrap = snap.edges[snap.edges["channel"] == ChannelTag.COMPOSABILITY_WRAP.value]
 assert len(wrap) == 1
 assert wrap.iloc[0]["weight"] == pytest.approx(0.0)


def test_pool_depth_edges_are_as_of_causal:
 events = [_core_event(100, 0, "Deposit", "0xu1", WETH, str(1 * 10**18))]
 engine = _engine(events)
 oracle = _StubPriceOracle({WETH: 2000.0})

 pool_bars = _pool_bars(
 "0xpool1",
 "WETH",
 [
 ("2020-12-31T00:00:00Z", 1_000_000.0),
 ("2021-01-05T00:00:00Z", 9_999_999.0), # future -- must not leak in
 ],
 )

 snap = build_snapshot(
 engine, oracle, 100, TS, top_n_whales=10, pool_depth_bars=pool_bars
 )
 depth_edges = snap.edges[snap.edges["channel"] == ChannelTag.POOL_DEPTH.value]
 assert len(depth_edges) == 1
 assert depth_edges.iloc[0]["dst_id"] == "pool:0xpool1"
 assert depth_edges.iloc[0]["weight"] == pytest.approx(1_000_000.0)
 assert "pool:0xpool1" in set(snap.nodes["node_id"])


def test_pool_with_no_prior_bar_is_dropped_not_zero_filled:
 events = [_core_event(100, 0, "Deposit", "0xu1", WETH, str(1 * 10**18))]
 engine = _engine(events)
 oracle = _StubPriceOracle({WETH: 2000.0})

 pool_bars = _pool_bars("0xpool1", "WETH", [("2021-06-01T00:00:00Z", 1_000_000.0)])
 snap = build_snapshot(
 engine, oracle, 100, TS, top_n_whales=10, pool_depth_bars=pool_bars
 )
 depth_edges = snap.edges[snap.edges["channel"] == ChannelTag.POOL_DEPTH.value]
 assert depth_edges.empty
 assert "pool:0xpool1" not in set(snap.nodes["node_id"])


def test_build_snapshot_before_any_event_has_no_position_nodes:
 events = [_core_event(100, 0, "Deposit", "0xu1", WETH, str(1 * 10**18))]
 engine = _engine(events)
 oracle = _StubPriceOracle({WETH: 2000.0})

 snap = build_snapshot(engine, oracle, 50, TS, top_n_whales=10)
 assert snap.nodes[
 snap.nodes["node_type"].isin(
 [NodeType.WHALE_POSITION.value, NodeType.POSITION_BUCKET.value]
 )
 ].empty
 assert "protocol:aave_v2" in set(snap.nodes["node_id"])


def test_build_snapshots_over_multiple_blocks:
 events = [
 _core_event(100, 0, "Deposit", "0xu1", WETH, str(1 * 10**18)),
 _core_event(200, 0, "Deposit", "0xu1", WETH, str(1 * 10**18)),
 ]
 engine = _engine(events)
 oracle = _StubPriceOracle({WETH: 1000.0})

 blocks = [100, 200]
 timestamps = pd.Series([TS, TS])
 snaps = build_snapshots(engine, oracle, blocks, timestamps, top_n_whales=10)
 assert [s.block for s in snaps] == [100, 200]
 assert snaps[0].nodes.set_index("node_id").loc[
 "whale:0xu1", "collateral_usd"
 ] == pytest.approx(1000.0)
 assert snaps[1].nodes.set_index("node_id").loc[
 "whale:0xu1", "collateral_usd"
 ] == pytest.approx(2000.0)
