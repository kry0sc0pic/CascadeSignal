"""Unit tests for SVR liquidation-path labeling."""

from __future__ import annotations

import pandas as pd

from cascadesignal.state.svr import SVR_REGIME_START, label_svr_routed

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
DAI = "0x6b175474e89094c44da98b954eedeac495271d0f" # not an SVR-covered reserve


def _liq(block_number: int, timestamp: str, collateral: str, debt: str) -> dict:
 return {
 "block_number": block_number,
 "block_timestamp": pd.Timestamp(timestamp, tz="UTC"),
 "collateral_asset": collateral,
 "debt_asset": debt,
 }


def _svr_event(block_number: int, asset: str) -> dict:
 return {"block_number": block_number, "collateral_asset": asset}


def test_pre_regime_liquidations_never_labeled:
 # Same-block match, but before SVR_REGIME_START -- must not be labeled,
 # since Aave's SVR pilot hadn't launched yet.
 liquidations = pd.DataFrame([_liq(100, "2025-01-15T00:00:00", WETH, USDC)])
 svr_events = pd.DataFrame([_svr_event(100, WETH)])
 result = label_svr_routed(liquidations, svr_events)
 assert result.tolist == [False]


def test_same_block_match_post_regime_labeled_true:
 ts = (SVR_REGIME_START + pd.Timedelta(days=10)).isoformat
 liquidations = pd.DataFrame([_liq(500, ts, WETH, USDC)])
 svr_events = pd.DataFrame([_svr_event(500, WETH)])
 result = label_svr_routed(liquidations, svr_events)
 assert result.tolist == [True]


def test_debt_asset_match_also_counts:
 ts = (SVR_REGIME_START + pd.Timedelta(days=10)).isoformat
 # collateral_asset (DAI) is not SVR-covered, but debt_asset (USDC) is,
 # and its SVR proxy fired in the same block.
 liquidations = pd.DataFrame([_liq(500, ts, DAI, USDC)])
 svr_events = pd.DataFrame([_svr_event(500, USDC)])
 result = label_svr_routed(liquidations, svr_events)
 assert result.tolist == [True]


def test_no_same_block_event_labeled_false:
 ts = (SVR_REGIME_START + pd.Timedelta(days=10)).isoformat
 liquidations = pd.DataFrame([_liq(500, ts, WETH, USDC)])
 # SVR event exists for WETH, but in a different block.
 svr_events = pd.DataFrame([_svr_event(501, WETH)])
 result = label_svr_routed(liquidations, svr_events)
 assert result.tolist == [False]


def test_uncovered_asset_never_labeled_even_with_block_match:
 ts = (SVR_REGIME_START + pd.Timedelta(days=10)).isoformat
 liquidations = pd.DataFrame([_liq(500, ts, DAI, DAI)])
 svr_events = pd.DataFrame([_svr_event(500, WETH)])
 result = label_svr_routed(liquidations, svr_events)
 assert result.tolist == [False]


def test_multiple_svr_assets_and_liquidations_labeled_independently:
 ts = (SVR_REGIME_START + pd.Timedelta(days=10)).isoformat
 liquidations = pd.DataFrame(
 [
 _liq(500, ts, WETH, DAI), # matches WETH SVR event same block
 _liq(501, ts, USDC, DAI), # no matching event at block 501
 _liq(502, ts, DAI, USDC), # matches USDC SVR event via debt_asset
 ]
 )
 svr_events = pd.DataFrame([_svr_event(500, WETH), _svr_event(502, USDC)])
 result = label_svr_routed(liquidations, svr_events)
 assert result.tolist == [True, False, True]


def test_empty_svr_events_labels_everything_false:
 ts = (SVR_REGIME_START + pd.Timedelta(days=10)).isoformat
 liquidations = pd.DataFrame([_liq(500, ts, WETH, USDC)])
 svr_events = pd.DataFrame(columns=["block_number", "collateral_asset"])
 result = label_svr_routed(liquidations, svr_events)
 assert result.tolist == [False]
