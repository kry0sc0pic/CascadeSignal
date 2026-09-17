"""Tests for The Graph ingestion layer.

Covers normalization, checkpointing, and query-budget bookkeeping without
hitting the network (the ingester's HTTP calls are exercised separately once
a working `GRAPH_API_KEY` is available -- see source_status.md).
"""

from __future__ import annotations

import json
import time

import pandas as pd
import pytest

from cascadesignal.ingest.collateral_pools import COLLATERAL_POOLS, WETH
from cascadesignal.ingest.thegraph import (
 CURVE_SCHEMA,
 UNISWAP_V2_SCHEMA,
 UNISWAP_V3_SCHEMA,
 QueryBudget,
 TheGraphIngester,
)


@pytest.fixture
def ingester(tmp_path, monkeypatch):
 monkeypatch.setenv("GRAPH_API_KEY", "test-key")
 return TheGraphIngester(data_dir=tmp_path)


def test_requires_api_key(tmp_path, monkeypatch):
 monkeypatch.delenv("GRAPH_API_KEY", raising=False)
 with pytest.raises(ValueError):
 TheGraphIngester(data_dir=tmp_path)


def test_normalize_uniswap_v2(ingester):
 rows = [
 {
 "date": 1609459200,
 "reserve0": "100.5",
 "reserve1": "50.25",
 "reserveUSD": "200000.0",
 "dailyVolumeToken0": "10.0",
 "dailyVolumeToken1": "5.0",
 "dailyVolumeUSD": "20000.0",
 }
 ]
 df = ingester._normalize("uniswap_v2", rows, "0xpool", "WBTC", 1, None)
 assert list(df.columns) == [f.name for f in UNISWAP_V2_SCHEMA]
 assert df.iloc[0]["reserve_usd"] == pytest.approx(200000.0)
 assert df.iloc[0]["pool_address"] == "0xpool"
 assert df.iloc[0]["collateral_symbol"] == "WBTC"


def test_normalize_uniswap_v3(ingester):
 rows = [
 {
 "date": 1609459200,
 "liquidity": "123456789012345678901234567890",
 "sqrtPrice": "987654321098765432109876543210",
 "token0Price": "20000.5",
 "token1Price": "0.00005",
 "tick": "-12345",
 "tvlUSD": "5000000.0",
 "volumeToken0": "1.5",
 "volumeToken1": "30000.0",
 "volumeUSD": "30000.0",
 },
 {
 # A day with no swaps has a null tick -- must not crash normalization.
 "date": 1609545600,
 "liquidity": "123456789012345678901234567890",
 "sqrtPrice": "987654321098765432109876543210",
 "token0Price": "20000.5",
 "token1Price": "0.00005",
 "tick": None,
 "tvlUSD": "5000000.0",
 "volumeToken0": "0.0",
 "volumeToken1": "0.0",
 "volumeUSD": "0.0",
 },
 ]
 df = ingester._normalize("uniswap_v3", rows, "0xpool3", "WBTC", 1, 3000)
 assert list(df.columns) == [f.name for f in UNISWAP_V3_SCHEMA]
 # Big ints must survive as strings, not get truncated/overflowed as floats.
 assert df.iloc[0]["liquidity"] == "123456789012345678901234567890"
 assert df.iloc[0]["fee_tier"] == 3000
 assert df.iloc[0]["tick"] == -12345
 assert pd.isna(df.iloc[1]["tick"])


def test_normalize_curve(ingester):
 rows = [
 {
 "timestamp": 1609459200,
 "totalValueLockedUSD": "80000000.0",
 "dailyVolumeUSD": "1200000.0",
 "inputTokenBalances": [
 "40000000000000000000000",
 "39000000000000000000000",
 ],
 }
 ]
 df = ingester._normalize("curve", rows, "0xcurvepool", "stETH", 1, None)
 assert list(df.columns) == [f.name for f in CURVE_SCHEMA]
 assert json.loads(df.iloc[0]["input_token_balances_raw"]) == [
 "40000000000000000000000",
 "39000000000000000000000",
 ]


def test_checkpoint_round_trip(ingester):
 ingester._save_checkpoint("uniswap_v2_0xpool", {"last_date": 1620000000})
 assert ingester._load_checkpoint("uniswap_v2_0xpool") == {"last_date": 1620000000}
 assert ingester._load_checkpoint("does_not_exist") == {}


def test_query_budget_tracks_and_enforces_limit(tmp_path):
 budget_path = tmp_path / "budget.json"
 budget = QueryBudget(budget_path)
 start_remaining = budget.remaining
 budget.record(10)
 assert budget.remaining == start_remaining - 10

 # Exhaust it and confirm check raises.
 budget.record(budget.remaining)
 with pytest.raises(RuntimeError):
 budget.check


def test_query_budget_resets_on_month_rollover(tmp_path):
 budget_path = tmp_path / "budget.json"
 budget_path.write_text(json.dumps({"month": "2000-01", "queries": 99_999}))
 budget = QueryBudget(budget_path)
 # Stale month from the file must not carry over into this month's count.
 assert budget._state["month"] == time.strftime("%Y-%m", time.gmtime)
 assert budget._state["queries"] == 0


def test_collateral_pools_have_lowercase_valid_addresses:
 for symbol, mapping in COLLATERAL_POOLS.items:
 assert mapping.collateral_asset == mapping.collateral_asset.lower
 assert mapping.collateral_asset.startswith("0x")
 assert len(mapping.collateral_asset) == 42
 for pool_addr in (
 mapping.uniswap_v2_pair,
 mapping.uniswap_v3_pool,
 mapping.curve_pool,
 ):
 if pool_addr is not None:
 assert pool_addr == pool_addr.lower
 assert pool_addr.startswith("0x")
 assert len(pool_addr) == 42


def test_collateral_pools_every_entry_has_at_least_one_venue:
 for symbol, mapping in COLLATERAL_POOLS.items:
 assert any(
 [mapping.uniswap_v2_pair, mapping.uniswap_v3_pool, mapping.curve_pool]
 ), f"{symbol} has no mapped pool at all"


def test_weth_is_the_numeraire_not_a_collateral_pool_key_mismatch:
 assert COLLATERAL_POOLS["WETH"].collateral_asset == WETH
