"""Contagion-graph snapshot builder.

Builds one heterogeneous graph snapshot (nodes + channel-tagged edges) at a
given block, from already-reconstructed position state
(`state.engine.PositionStateEngine`), Aave's reserve registry,
and DEX pool depth (`graph.pool_depth`), applying its own whale/
top-N node universe and as-of causality contract, plus the position-bucket,
asset, pool, and protocol node types and the channel-tagged edges the contagion-graph spec
specifies around them.

T2 passed via ADR-005 (tolerance-band mismatch rate 1.093% at
epsilon=1%, against the 2% gate). `scripts/graph/materialize_snapshots.py`
re-checks the live T2 rate and runs the full (`build_snapshots` over the whole Aave v2 study period)
see that script's docstring for the gating/cadence details; its output lives
under `data/curated/graph/aave_v2/`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from cascadesignal.graph.pool_depth import depth_at
from cascadesignal.graph.schema import (
 COMPOSABILITY_WRAPS,
 ChannelTag,
 NodeType,
 hf_band_series,
)
from cascadesignal.state.engine import PositionStateEngine
from cascadesignal.state.t2_gate import PriceOracleLike

DEFAULT_SNAPSHOT_CADENCE_BLOCKS = 25 # PLAN §6
STRESS_SNAPSHOT_CADENCE_BLOCKS = 5 # PLAN §6, denser inside stress windows
DEFAULT_TOP_N_WHALES = 500 # PLAN §6: "top ~500 whale positions individually"
MAX_NODES_PER_SNAPSHOT = 10_000 # PLAN §6 node cap

_EPS = 1e-9

NODE_FEATURE_COLUMNS = (
 "node_id",
 "node_type",
 "label",
 "collateral_usd",
 "debt_usd",
 "health_factor",
 "n_positions",
)
EDGE_COLUMNS = ("src_id", "dst_id", "channel", "weight")


@dataclass(frozen=True)
class GraphSnapshot:
 block: int
 timestamp: pd.Timestamp
 protocol: str
 nodes: pd.DataFrame # NODE_FEATURE_COLUMNS + snapshot_block
 edges: pd.DataFrame # EDGE_COLUMNS + snapshot_block


def _asset_node_id(symbol: str) -> str:
 return f"asset:{symbol}"


def _pool_node_id(pool_address: str) -> str:
 return f"pool:{pool_address}"


def _protocol_node_id(protocol: str) -> str:
 return f"protocol:{protocol}"


def _position_level_frame(
 engine: PositionStateEngine,
 block: int,
 price_oracle: PriceOracleLike,
 timestamp: pd.Timestamp,
) -> pd.DataFrame:
 """Per-(user, reserve) USD-valued rows at `block` -- the granularity
 the per-user node aggregation collapses away but bucket assignment
 (dominant collateral asset per user) and per-asset exposure edges need.
 Vectorized.
 """
 positions = engine.positions_at(block)
 columns = [
 "user",
 "reserve",
 "symbol",
 "collateral_usd",
 "debt_usd",
 "weighted_collateral_usd",
 ]
 if positions.empty:
 return pd.DataFrame(columns=columns)

 reserves = positions["reserve"].unique.tolist
 prices = price_oracle.prices_at(reserves, timestamp)
 indexed = engine.reserve_table.set_index("address")
 thresholds = indexed["liquidation_threshold"].to_dict
 symbols = indexed["symbol"].to_dict

 price = positions["reserve"].map(prices)
 threshold = positions["reserve"].map(thresholds)
 symbol = positions["reserve"].map(symbols)

 collateral_units = positions["collateral_units"].clip(lower=0.0)
 debt_units = positions["debt_units"].clip(lower=0.0)
 has_collateral = collateral_units > _EPS
 has_debt = debt_units > _EPS
 price_known = price.notna

 collateral_usd = (collateral_units * price).where(has_collateral & price_known, 0.0)
 debt_usd = (debt_units * price).where(has_debt & price_known, 0.0)
 weighted_collateral_usd = (collateral_usd * threshold).where(
 has_collateral & threshold.notna, 0.0
 )

 return pd.DataFrame(
 {
 "user": positions["user"].to_numpy,
 "reserve": positions["reserve"].to_numpy,
 "symbol": symbol.to_numpy,
 "collateral_usd": collateral_usd.to_numpy,
 "debt_usd": debt_usd.to_numpy,
 "weighted_collateral_usd": weighted_collateral_usd.to_numpy,
 }
 )


def _user_aggregate(position_frame: pd.DataFrame) -> pd.DataFrame:
 """Per-user rollup: total collateral/debt USD, pooled health factor, and
 the user's dominant (largest-USD) collateral reserve symbol -- the
 bucket-key input. Deterministic tie-break on reserve address."""
 columns = [
 "user",
 "collateral_usd",
 "debt_usd",
 "weighted_collateral_usd",
 "health_factor",
 "dominant_symbol",
 ]
 if position_frame.empty:
 return pd.DataFrame(columns=columns)

 g = position_frame.groupby("user", sort=False)
 collateral_total = g["collateral_usd"].sum
 debt_total = g["debt_usd"].sum
 weighted_total = g["weighted_collateral_usd"].sum

 dominant = (
 position_frame.sort_values(
 ["user", "collateral_usd", "reserve"],
 ascending=[True, False, True],
 kind="mergesort",
 )
 .groupby("user", sort=False)
 .first["symbol"]
 )

 agg = pd.DataFrame(
 {
 "collateral_usd": collateral_total,
 "debt_usd": debt_total,
 "weighted_collateral_usd": weighted_total,
 "dominant_symbol": dominant,
 }
 ).reset_index
 agg["health_factor"] = np.where(
 agg["debt_usd"].to_numpy > _EPS,
 agg["weighted_collateral_usd"].to_numpy
 / np.clip(agg["debt_usd"].to_numpy, _EPS, None),
 np.nan,
 )
 return agg[columns]


def _assign_node_ids(user_agg: pd.DataFrame, top_n: int, market: str) -> pd.DataFrame:
 """Rank users by collateral USD (ties broken by address, deterministic);
 the top `top_n` each become an individual whale node, the rest are
 bucketed by (market, dominant collateral symbol, HF band)."""
 if user_agg.empty:
 return user_agg.assign(
 is_whale=pd.Series(dtype=bool), node_id=pd.Series(dtype=str)
 )

 ranked = user_agg.sort_values(
 ["collateral_usd", "user"], ascending=[False, True], kind="mergesort"
 ).reset_index(drop=True)
 ranked["is_whale"] = ranked.index < top_n
 band = hf_band_series(ranked["health_factor"])
 whale_ids = "whale:" + ranked["user"]
 bucket_ids = (
 "bucket:"
 + market
 + ":"
 + ranked["dominant_symbol"].astype(str)
 + ":"
 + band.astype(str)
 )
 ranked["node_id"] = np.where(ranked["is_whale"], whale_ids, bucket_ids)
 return ranked


def _position_nodes(ranked: pd.DataFrame) -> pd.DataFrame:
 """Whale nodes (one per user) + bucket nodes (aggregated, HF pooled from
 summed weighted collateral / summed debt, not averaged ratios)."""
 empty = pd.DataFrame(columns=list(NODE_FEATURE_COLUMNS))
 if ranked.empty:
 return empty

 whales = ranked[ranked["is_whale"]].copy
 whales["node_type"] = NodeType.WHALE_POSITION.value
 whales["label"] = whales["user"]
 whales["n_positions"] = 1
 whale_nodes = whales[list(NODE_FEATURE_COLUMNS)]

 rest = ranked[~ranked["is_whale"]]
 if rest.empty:
 return whale_nodes.reset_index(drop=True)

 bucket_nodes = (
 rest.groupby("node_id", sort=False)
 .agg(
 collateral_usd=("collateral_usd", "sum"),
 debt_usd=("debt_usd", "sum"),
 weighted_collateral_usd=("weighted_collateral_usd", "sum"),
 n_positions=("user", "count"),
 )
 .reset_index
 )
 bucket_nodes["health_factor"] = np.where(
 bucket_nodes["debt_usd"].to_numpy > _EPS,
 bucket_nodes["weighted_collateral_usd"].to_numpy
 / np.clip(bucket_nodes["debt_usd"].to_numpy, _EPS, None),
 np.nan,
 )
 bucket_nodes["node_type"] = NodeType.POSITION_BUCKET.value
 bucket_nodes["label"] = bucket_nodes["node_id"]
 bucket_nodes = bucket_nodes[list(NODE_FEATURE_COLUMNS)]

 return pd.concat([whale_nodes, bucket_nodes], ignore_index=True)


def _exposure_edges(
 position_frame: pd.DataFrame, node_of_user: pd.Series
) -> pd.DataFrame:
 """COLLATERAL_EXPOSURE / DEBT_EXPOSURE edges from every whale/bucket node
 to the asset nodes it's exposed to, USD-weighted. Works uniformly for
 both node kinds since `node_of_user` already resolved each user to its
 whale-or-bucket node id."""
 if position_frame.empty:
 return pd.DataFrame(columns=list(EDGE_COLUMNS))

 frame = position_frame.copy
 frame["node_id"] = frame["user"].map(node_of_user)
 frame["asset_id"] = "asset:" + frame["symbol"].astype(str)

 parts = []
 for value_col, channel in (
 ("collateral_usd", ChannelTag.COLLATERAL_EXPOSURE.value),
 ("debt_usd", ChannelTag.DEBT_EXPOSURE.value),
 ):
 grouped = (
 frame[frame[value_col] > _EPS]
 .groupby(["node_id", "asset_id"], sort=False)[value_col]
 .sum
 .reset_index
 )
 grouped["channel"] = channel
 grouped = grouped.rename(
 columns={"node_id": "src_id", "asset_id": "dst_id", value_col: "weight"}
 )
 parts.append(grouped[list(EDGE_COLUMNS)])

 return (
 pd.concat(parts, ignore_index=True)
 if parts
 else pd.DataFrame(columns=list(EDGE_COLUMNS))
 )


def _asset_nodes(symbols: set[str]) -> pd.DataFrame:
 rows = [
 {
 "node_id": _asset_node_id(sym),
 "node_type": NodeType.ASSET.value,
 "label": sym,
 "collateral_usd": np.nan,
 "debt_usd": np.nan,
 "health_factor": np.nan,
 "n_positions": 0,
 }
 for sym in sorted(symbols)
 ]
 return pd.DataFrame(rows, columns=list(NODE_FEATURE_COLUMNS))


def _pool_nodes_and_edges(
 pool_depth_now: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
 if pool_depth_now.empty:
 return (
 pd.DataFrame(columns=list(NODE_FEATURE_COLUMNS)),
 pd.DataFrame(columns=list(EDGE_COLUMNS)),
 )

 nodes = pd.DataFrame(
 {
 "node_id": ("pool:" + pool_depth_now["pool_address"]).to_numpy,
 "node_type": NodeType.POOL.value,
 "label": pool_depth_now["pool_address"].to_numpy,
 "collateral_usd": np.nan,
 "debt_usd": np.nan,
 "health_factor": np.nan,
 "n_positions": 0,
 },
 columns=list(NODE_FEATURE_COLUMNS),
 )
 edges = pd.DataFrame(
 {
 "src_id": ("asset:" + pool_depth_now["collateral_symbol"]).to_numpy,
 "dst_id": ("pool:" + pool_depth_now["pool_address"]).to_numpy,
 "channel": ChannelTag.POOL_DEPTH.value,
 "weight": pool_depth_now["depth_usd"].to_numpy,
 },
 columns=list(EDGE_COLUMNS),
 )
 return nodes, edges


def _composability_edges(
 asset_symbols: set[str], collateral_usd_by_symbol: dict[str, float]
) -> pd.DataFrame:
 rows = [
 {
 "src_id": _asset_node_id(wrapped),
 "dst_id": _asset_node_id(underlying),
 "channel": ChannelTag.COMPOSABILITY_WRAP.value,
 "weight": collateral_usd_by_symbol.get(wrapped, 0.0),
 }
 for wrapped, underlying in COMPOSABILITY_WRAPS.items
 if wrapped in asset_symbols
 ]
 return pd.DataFrame(rows, columns=list(EDGE_COLUMNS))


def build_snapshot(
 engine: PositionStateEngine,
 price_oracle: PriceOracleLike,
 block: int,
 timestamp: pd.Timestamp,
 *,
 protocol: str = "aave_v2",
 market: str = "aave_v2",
 top_n_whales: int = DEFAULT_TOP_N_WHALES,
 pool_depth_bars: pd.DataFrame | None = None,
) -> GraphSnapshot:
 """Build one heterogeneous contagion-graph snapshot at `block`.

 `pool_depth_bars` is `pool_depth.load_pool_depth_bars`'s output (or
 `None` to skip pool/pool-depth nodes and edges entirely, e.g. in tests
 that don't exercise that channel). Deterministic: no randomness in
 ranking, bucketing, or ordering.
 """
 position_frame = _position_level_frame(engine, block, price_oracle, timestamp)
 user_agg = _user_aggregate(position_frame)
 ranked = _assign_node_ids(user_agg, top_n_whales, market)
 position_nodes = _position_nodes(ranked)

 node_of_user = (
 ranked.set_index("user")["node_id"]
 if not ranked.empty
 else pd.Series(dtype=str)
 )
 exposure_edges = _exposure_edges(position_frame, node_of_user)

 touched_symbols = set(position_frame["symbol"].dropna.unique)
 wrap_symbols = set(COMPOSABILITY_WRAPS) | set(COMPOSABILITY_WRAPS.values)
 asset_symbols = touched_symbols | wrap_symbols
 collateral_by_symbol = position_frame.groupby("symbol")["collateral_usd"].sum
 collateral_usd_by_symbol: dict[str, float] = {
 str(symbol): float(usd) for symbol, usd in collateral_by_symbol.items
 }

 pool_depth_now = pd.DataFrame(
 columns=["dex", "pool_address", "collateral_symbol", "date", "depth_usd"]
 )
 if pool_depth_bars is not None and not pool_depth_bars.empty:
 pool_depth_now = depth_at(pool_depth_bars, timestamp)
 asset_symbols |= set(pool_depth_now["collateral_symbol"].unique)

 asset_nodes = _asset_nodes(asset_symbols)
 pool_nodes, pool_edges = _pool_nodes_and_edges(pool_depth_now)
 wrap_edges = _composability_edges(asset_symbols, collateral_usd_by_symbol)

 protocol_nodes = pd.DataFrame(
 [
 {
 "node_id": _protocol_node_id(protocol),
 "node_type": NodeType.PROTOCOL.value,
 "label": protocol,
 "collateral_usd": np.nan,
 "debt_usd": np.nan,
 "health_factor": np.nan,
 "n_positions": 0,
 }
 ],
 columns=list(NODE_FEATURE_COLUMNS),
 )

 nodes = pd.concat(
 [position_nodes, asset_nodes, pool_nodes, protocol_nodes], ignore_index=True
 )
 if len(nodes) > MAX_NODES_PER_SNAPSHOT:
 raise ValueError(
 f"snapshot at block {block} has {len(nodes)} nodes, over the "
 f"PLAN §6 cap of {MAX_NODES_PER_SNAPSHOT}"
 )
 nodes["snapshot_block"] = block

 edges = pd.concat([exposure_edges, pool_edges, wrap_edges], ignore_index=True)
 edges["snapshot_block"] = block

 return GraphSnapshot(
 block=block, timestamp=timestamp, protocol=protocol, nodes=nodes, edges=edges
 )


def build_snapshots(
 engine: PositionStateEngine,
 price_oracle: PriceOracleLike,
 blocks: np.ndarray,
 timestamps: pd.Series,
 **kwargs,
) -> list[GraphSnapshot]:
 """`build_snapshot` over an explicit block/timestamp sequence (typically
 from `stress_windows.snapshot_blocks_with_stress`)."""
 return [
 build_snapshot(engine, price_oracle, int(block), ts, **kwargs)
 for block, ts in zip(blocks, timestamps)
 ]


__all__ = [
 "GraphSnapshot",
 "build_snapshot",
 "build_snapshots",
 "DEFAULT_SNAPSHOT_CADENCE_BLOCKS",
 "STRESS_SNAPSHOT_CADENCE_BLOCKS",
 "DEFAULT_TOP_N_WHALES",
 "MAX_NODES_PER_SNAPSHOT",
 "NODE_FEATURE_COLUMNS",
 "EDGE_COLUMNS",
]
