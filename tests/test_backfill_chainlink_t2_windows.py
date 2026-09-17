"""Tests for the surgical Chainlink T2-window backfill planner.

Covers the offline, no-network pieces of
`scripts/onchain/backfill_chainlink_t2_windows.py`:

- `merge_windows`: overlapping trigger blocks collapse into disjoint
 `[block - span, block]` ranges (the request-count savings the pull relies on).
- `_rows_from_logs`: a raw Etherscan `AnswerUpdated` log becomes a
 canonical-schema row `ChainlinkPriceOracle` can read (incl. two's-complement
 int256 decode).
- `build_pull_plan`: only *fixable* `unexplained_no_chainlink_coverage`
 triggers contribute; a position touching an `UNCOVERED_RESERVES` asset is
 skipped, and an ETH-quoted debt reserve also pulls the ETH/USD aggregator.

The live pull (`_pull_aggregator` / `main`) is network-bound (Etherscan) and
not exercised here -- same convention as `fetch_svr_feed_events` having no
network test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve.parent.parent / "scripts" / "onchain"))

import backfill_chainlink_t2_windows as bf # noqa: E402

from cascadesignal.state.chainlink_feeds import ( # noqa: E402
 ETH_USD_AGGREGATORS,
 RESERVE_CHAINLINK_FEEDS,
)
from cascadesignal.state.engine import PositionStateEngine # noqa: E402
from cascadesignal.state.reserves import reserve_table # noqa: E402

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
STETH = "0xae7ab96520de3a18e5e111b5eaab095312d7fe84" # UNCOVERED_RESERVES, no feed
BAL = "0xba100000625a3754423978a60c9317c58a424e3d" # quote == "ETH"

RESERVES = reserve_table


class _StubOracle:
 def __init__(self, prices: dict[str, float]):
 self._prices = prices

 def prices_at(
 self,
 addresses: list[str],
 timestamp: pd.Timestamp,
 block_number: int | None = None,
 log_index: int | None = None,
 ) -> dict[str, float]:
 return {a: self._prices[a] for a in addresses if a in self._prices}


def _core_event(block, log_index, event_type, user, reserve, amount_raw):
 return {
 "chain_id": 1,
 "block_number": block,
 "block_timestamp": pd.Timestamp("2022-01-01", tz="UTC")
 + pd.Timedelta(seconds=block),
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
 }


def _liquidation_event(
 block, log_index, user, collateral_asset, debt_asset, debt_raw, seized_raw
):
 return {
 "chain_id": 1,
 "block_number": block,
 "block_timestamp": pd.Timestamp("2022-01-01", tz="UTC")
 + pd.Timedelta(seconds=block),
 "log_index": log_index,
 "tx_hash": f"0xtx{block}_{log_index}",
 "protocol": "aave_v2",
 "event_type": "LiquidationCall",
 "user": user,
 "collateral_asset": collateral_asset,
 "debt_asset": debt_asset,
 "amount_raw": debt_raw,
 "amount_usd": None,
 "liquidator": "0xliquidator",
 "collateral_seized_raw": seized_raw,
 "collateral_seized_usd": None,
 }


# ---------------------------------------------------------------------------
# merge_windows
# ---------------------------------------------------------------------------


def test_merge_windows_merges_overlapping_keeps_disjoint:
 # 5000 -> [4000,5000]; 5500 overlaps (4500 <= 5000) -> extend to [4000,5500];
 # 20000 is far -> its own [19000,20000].
 assert bf.merge_windows([5500, 5000, 20000], span=1000) == [
 [4000, 5500],
 [19000, 20000],
 ]


def test_merge_windows_dedupes_and_handles_singletons:
 assert bf.merge_windows([], span=1000) == []
 assert bf.merge_windows([100, 100], span=1000) == [[-900, 100]]


# ---------------------------------------------------------------------------
# _rows_from_logs
# ---------------------------------------------------------------------------


def test_rows_from_logs_canonical_schema_and_decode:
 log = {
 "blockNumber": hex(15_000_000),
 "timeStamp": hex(1_650_000_000),
 "transactionHash": "0xabc",
 "logIndex": hex(7),
 "topics": ["0xtopic0", hex(3000 * 10**8)], # $3000 at 8 decimals
 }
 (row,) = bf._rows_from_logs("0xAggReGaTor", decimals=8, logs=[log])
 assert row["protocol"] == "chainlink"
 assert row["event_type"] == "AnswerUpdated"
 assert row["user"] == "0xaggregator" # lowercased
 assert row["block_number"] == 15_000_000
 assert row["amount_raw"] == str(3000 * 10**8)
 assert row["amount_usd"] == 3000.0
 assert row["block_timestamp"] == pd.Timestamp(1_650_000_000, unit="s", tz="UTC")


def test_rows_from_logs_decodes_negative_twos_complement:
 log = {
 "blockNumber": hex(1),
 "timeStamp": hex(1),
 "transactionHash": "0x0",
 "logIndex": hex(0),
 "topics": ["0xtopic0", hex(2**256 - 5)], # int256 = -5
 }
 (row,) = bf._rows_from_logs("0xagg", decimals=8, logs=[log])
 assert row["amount_raw"] == "-5"


# ---------------------------------------------------------------------------
# build_pull_plan
# ---------------------------------------------------------------------------


def _plan_for(events: pd.DataFrame) -> dict[str, dict]:
 engine = PositionStateEngine(events=events, reserve_table=RESERVES)
 liquidations = events[events["event_type"] == "LiquidationCall"]
 # Generous prices force HF >= 1 (a mismatch); empty Chainlink stub forces
 # every mismatch into the `unexplained_no_chainlink_coverage` bucket.
 primary = _StubOracle({WETH: 5000.0, USDC: 1.0, STETH: 5000.0, BAL: 20.0})
 chainlink = _StubOracle({})
 return bf.build_pull_plan(engine, liquidations, primary, chainlink)


def test_build_pull_plan_targets_fixable_skips_uncovered_and_adds_eth_usd:
 # Blocks are spaced > STALENESS_BLOCKS apart, so each window's `hi` is
 # exactly its trigger block (no cross-trigger merge) -- lets us assert which
 # triggers made it into the plan by their block numbers.
 b_fix, b_eth, b_unfix = 100_000, 300_000, 500_000
 events = pd.DataFrame(
 [
 # fixable: WETH collateral (reliable) + USDC debt, both have feeds
 _core_event(b_fix - 2, 0, "Deposit", "u_fix", WETH, str(1 * 10**18)),
 _core_event(b_fix - 1, 0, "Borrow", "u_fix", USDC, str(100 * 10**6)),
 _liquidation_event(b_fix, 0, "u_fix", WETH, USDC, str(50 * 10**6), str(0)),
 # ETH-quoted debt: WETH collateral + BAL debt (BAL quote == "ETH")
 _core_event(b_eth - 2, 0, "Deposit", "u_eth", WETH, str(1 * 10**18)),
 _core_event(b_eth - 1, 0, "Borrow", "u_eth", BAL, str(1 * 10**18)),
 _liquidation_event(b_eth, 0, "u_eth", WETH, BAL, str(1 * 10**17), str(0)),
 # unfixable: stETH collateral has no Chainlink feed -> whole position skipped
 _core_event(b_unfix - 2, 0, "Deposit", "u_unfix", STETH, str(1 * 10**18)),
 _core_event(b_unfix - 1, 0, "Borrow", "u_unfix", USDC, str(100 * 10**6)),
 _liquidation_event(
 b_unfix, 0, "u_unfix", STETH, USDC, str(50 * 10**6), str(0)
 ),
 ]
 )
 plan = _plan_for(events)

 trigger_his = {hi for spec in plan.values for _lo, hi in spec["windows"]}
 assert b_fix in trigger_his # fixable pulled
 assert b_eth in trigger_his # ETH-quoted fixable pulled
 assert b_unfix not in trigger_his # uncovered-reserve position skipped

 # USDC + WETH aggregators present from the fixable trigger.
 for agg in RESERVE_CHAINLINK_FEEDS[USDC]["aggregators"]:
 assert agg.lower in plan
 assert RESERVE_CHAINLINK_FEEDS[WETH]["aggregators"][0].lower in plan

 # BAL's own aggregator AND the ETH/USD aggregator (needed to convert
 # BAL/ETH -> USD) are both in the plan.
 assert RESERVE_CHAINLINK_FEEDS[BAL]["aggregators"][0].lower in plan
 assert ETH_USD_AGGREGATORS[0].lower in plan
 assert plan[ETH_USD_AGGREGATORS[0].lower]["symbol"] in {"WETH", "ETH/USD"}

 # stETH has no feed, so no aggregator in the plan carries its price.
 assert STETH not in plan


def test_build_pull_plan_empty_when_no_mismatches:
 # A correctly-insolvent liquidation (tiny collateral, large debt) is not a
 # mismatch, so nothing to backfill.
 events = pd.DataFrame(
 [
 _core_event(98, 0, "Deposit", "u_ok", WETH, str(1 * 10**18)),
 _core_event(99, 0, "Borrow", "u_ok", USDC, str(100_000 * 10**6)),
 _liquidation_event(100, 0, "u_ok", WETH, USDC, str(1 * 10**6), str(0)),
 ]
 )
 assert _plan_for(events) == {}
