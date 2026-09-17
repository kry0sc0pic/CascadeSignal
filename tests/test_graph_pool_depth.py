"""Unit tests for DEX pool-depth as-of lookup."""

from __future__ import annotations

import pandas as pd
import pytest

from cascadesignal.graph.pool_depth import (
 POOL_BARS_COLUMNS,
 depth_at,
 load_pool_depth_bars,
)


def _bars(rows):
 return pd.DataFrame(
 {
 "dex": [r[0] for r in rows],
 "pool_address": [r[1] for r in rows],
 "collateral_symbol": [r[2] for r in rows],
 "date": pd.to_datetime([r[3] for r in rows], utc=True),
 "depth_usd": [r[4] for r in rows],
 }
 )


def test_load_pool_depth_bars_empty_dir_returns_empty_frame(tmp_path):
 df = load_pool_depth_bars(data_dir=tmp_path)
 assert df.empty
 assert list(df.columns) == POOL_BARS_COLUMNS


def test_depth_at_picks_nearest_prior_day_per_pool:
 bars = _bars(
 [
 ("uniswap_v2", "0xpoolA", "WETH", "2021-01-01T00:00:00Z", 100.0),
 ("uniswap_v2", "0xpoolA", "WETH", "2021-01-03T00:00:00Z", 300.0),
 ("uniswap_v2", "0xpoolA", "WETH", "2021-01-10T00:00:00Z", 999.0), # future
 ]
 )
 out = depth_at(bars, pd.Timestamp("2021-01-05T00:00:00Z"))
 assert len(out) == 1
 assert out.iloc[0]["depth_usd"] == pytest.approx(300.0)


def test_depth_at_drops_pool_with_no_prior_bar:
 bars = _bars([("uniswap_v2", "0xpoolA", "WETH", "2021-06-01T00:00:00Z", 100.0)])
 out = depth_at(bars, pd.Timestamp("2021-01-01T00:00:00Z"))
 assert out.empty


def test_depth_at_handles_empty_input:
 empty = pd.DataFrame(columns=POOL_BARS_COLUMNS)
 out = depth_at(empty, pd.Timestamp("2021-01-01T00:00:00Z"))
 assert out.empty
