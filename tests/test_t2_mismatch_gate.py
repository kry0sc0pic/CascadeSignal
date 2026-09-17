"""Tests for the T2 state-reconstruction correctness gate.

Two layers:

1. Unit tests (below, always run) on synthetic fixtures -- exercise
 `cascadesignal.state.t2_gate`'s batch HF reconstruction, per-protocol
 mismatch summary, and cause-bucketing logic against hand-computed
 expectations, independent of real data.
2. The real T2 gate itself (`test_t2_gate_enforces_two_percent_mismatch`,
 bottom of file) -- skips cleanly without the LFS-pulled data lake (same
 convention as `tests/test_state_reconstruction.py`), but when it runs
 against the real Aave v2 liquidation population it enforces the T2 gate's
 actual invariant, as redefined by ADR-005's tolerance band: > 2%
 of measurable liquidations reconstructing to HF >= 1.0 + HF_TOLERANCE
 at trigger blocks the pipeline. Sixteen levers drove the exact-boundary
 rate from 53.9% to 2.5853% (see `experiments/T2/CAS28_mismatch_next_steps.md`
 for the full lever-by-lever history); at that point three independent
 ground-truth checks confirmed the residual was this reconstruction's own
 ~1% precision floor, not a fixable bug, so ADR-005 (mentor-accepted)
 pre-registered `HF_TOLERANCE=0.01` on top of the exact boundary. This
 test now PASSES (1.093% tolerance-band rate vs. the 2% bar) -- the first
 time in this ticket's history.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from cascadesignal.state.engine import PositionStateEngine, load_events
from cascadesignal.state.reserves import reserve_table
from cascadesignal.state.t2_gate import (
 HF_TOLERANCE,
 MISMATCH_THRESHOLD,
 apply_live_oracle_fallback,
 attach_diagnostics,
 bucket_mismatch_causes,
 mismatch_summary,
 reconstruct_hf_at_trigger,
)

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
YFI = "0x0bc529c00c6401aef6d220be8c6ea1667f6ad93e" # de-risked -> historical_reliable=False
LINK = "0x514910771af9ca656af840dff83e8264ecf986ca"
DATA_DIR = Path("data/raw")

RESERVES = reserve_table


def _has_real_parquet(directory: Path) -> bool:
 """True only if `directory` holds a real parquet file (PAR1 magic bytes),
 not a Git LFS pointer -- mirrors tests/test_state_reconstruction.py's guard."""
 if not directory.exists:
 return False
 for path in directory.rglob("*.parquet"):
 try:
 with open(path, "rb") as handle:
 if handle.read(4) == b"PAR1":
 return True
 except OSError:
 continue
 return False


_HAS_REAL_DATA = _has_real_parquet(DATA_DIR / "aave_v2")


class _StubOracle:
 """Fixed reserve -> price map; missing reserves surface as no-coverage,
 matching `PriceOracle`/`ChainlinkPriceOracle`'s `prices_at` contract."""

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


def _core_event(
 block, log_index, event_type, user, reserve, amount_raw, protocol="aave_v2"
):
 return {
 "chain_id": 1,
 "block_number": block,
 "block_timestamp": pd.Timestamp("2022-01-01", tz="UTC")
 + pd.Timedelta(seconds=block),
 "log_index": log_index,
 "tx_hash": f"0xtx{block}_{log_index}",
 "protocol": protocol,
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
 block,
 log_index,
 user,
 collateral_asset,
 debt_asset,
 debt_repaid_raw,
 collateral_seized_raw,
 protocol="aave_v2",
):
 return {
 "chain_id": 1,
 "block_number": block,
 "block_timestamp": pd.Timestamp("2022-01-01", tz="UTC")
 + pd.Timedelta(seconds=block),
 "log_index": log_index,
 "tx_hash": f"0xtx{block}_{log_index}",
 "protocol": protocol,
 "event_type": "LiquidationCall",
 "user": user,
 "collateral_asset": collateral_asset,
 "debt_asset": debt_asset,
 "amount_raw": debt_repaid_raw,
 "amount_usd": None,
 "liquidator": "0xliquidator",
 "collateral_seized_raw": collateral_seized_raw,
 "collateral_seized_usd": None,
 }


# ---------------------------------------------------------------------------
# reconstruct_hf_at_trigger
# ---------------------------------------------------------------------------


def test_reconstruct_matches_a_correctly_triggered_liquidation:
 # Pre-liquidation state (block 101): 2 WETH collateral, 6000 USDC debt.
 # Debt is set to 1.5x the collateral notional so weighted_collateral
 # (<= notional, since liquidation_threshold <= 1) can never catch up to
 # debt, whatever WETH's actual liquidation_threshold is -- a correctly
 # insolvent (HF < 1) position, so no mismatch.
 events = pd.DataFrame(
 [
 _core_event(100, 0, "Deposit", "u_match", WETH, str(2 * 10**18)),
 _core_event(101, 0, "Borrow", "u_match", USDC, str(6000 * 10**6)),
 _liquidation_event(
 102, 0, "u_match", WETH, USDC, str(3000 * 10**6), str(1 * 10**18)
 ),
 ]
 )
 engine = PositionStateEngine(events=events, reserve_table=RESERVES)
 liquidations = events[events["event_type"] == "LiquidationCall"]
 oracle = _StubOracle({WETH: 2000.0, USDC: 1.0})

 report, position_by_key = reconstruct_hf_at_trigger(engine, liquidations, oracle)

 assert len(report) == 1
 row = report.iloc[0]
 assert row["measurable"]
 assert not row["mismatch"]
 assert row["health_factor"] < 1.0
 assert ("u_match", 102, 0) in position_by_key


def test_reconstruct_flags_mismatch_when_hf_at_least_one:
 events = pd.DataFrame(
 [
 _core_event(100, 0, "Deposit", "u_bad", WETH, str(1 * 10**18)),
 _core_event(101, 0, "Borrow", "u_bad", USDC, str(100 * 10**6)),
 _liquidation_event(102, 0, "u_bad", WETH, USDC, str(50 * 10**6), str(0)),
 ]
 )
 engine = PositionStateEngine(events=events, reserve_table=RESERVES)
 liquidations = events[events["event_type"] == "LiquidationCall"]
 # Generous price -> collateral comfortably covers remaining debt post-trigger.
 oracle = _StubOracle({WETH: 5000.0, USDC: 1.0})

 report, _ = reconstruct_hf_at_trigger(engine, liquidations, oracle)

 row = report.iloc[0]
 assert row["measurable"]
 assert row["mismatch"]
 assert row["health_factor"] >= 1.0


def test_reconstruct_marks_missing_price_coverage_as_not_measurable:
 events = pd.DataFrame(
 [
 _core_event(100, 0, "Deposit", "u_gap", WETH, str(1 * 10**18)),
 _core_event(101, 0, "Borrow", "u_gap", USDC, str(100 * 10**6)),
 _liquidation_event(102, 0, "u_gap", WETH, USDC, str(50 * 10**6), str(0)),
 ]
 )
 engine = PositionStateEngine(events=events, reserve_table=RESERVES)
 liquidations = events[events["event_type"] == "LiquidationCall"]
 oracle = _StubOracle(
 {WETH: 5000.0}
 ) # no USDC price -> can't fully cover the debt leg

 report, _ = reconstruct_hf_at_trigger(engine, liquidations, oracle)

 row = report.iloc[0]
 assert not row["fully_covered"]
 assert not row["measurable"]


def test_reconstruct_full_repay_is_measurable_pre_liquidation:
 # Under the pre-liquidation convention, a liquidation that fully clears
 # the account's debt is STILL measurable: we evaluate the state entering
 # the trigger block (block 101), where the 100 USDC debt still exists.
 # (Under the old post-liquidation convention this was "not measurable"
 # because the repay had zeroed the debt -- see the module docstring.)
 events = pd.DataFrame(
 [
 _core_event(100, 0, "Deposit", "u_closed", WETH, str(1 * 10**18)),
 _core_event(101, 0, "Borrow", "u_closed", USDC, str(100 * 10**6)),
 _liquidation_event(
 102, 0, "u_closed", WETH, USDC, str(100 * 10**6), str(1 * 10**18)
 ),
 ]
 )
 engine = PositionStateEngine(events=events, reserve_table=RESERVES)
 liquidations = events[events["event_type"] == "LiquidationCall"]
 oracle = _StubOracle({WETH: 2000.0, USDC: 1.0})

 report, _ = reconstruct_hf_at_trigger(engine, liquidations, oracle)

 row = report.iloc[0]
 assert row["block_number"] == 102 # keyed on the true trigger block
 assert row["fully_covered"]
 assert row["health_factor"] is not None # pre-liq debt still present
 assert row["measurable"]


def test_reconstruct_same_block_open_and_liquidation_not_measurable:
 # A position opened (deposit + borrow) in the very same block it was
 # liquidated has no state entering that block, so the pre-liquidation
 # convention correctly marks it not measurable (no debt at block-1).
 events = pd.DataFrame(
 [
 _core_event(102, 0, "Deposit", "u_flash", WETH, str(1 * 10**18)),
 _core_event(102, 1, "Borrow", "u_flash", USDC, str(100 * 10**6)),
 _liquidation_event(102, 2, "u_flash", WETH, USDC, str(50 * 10**6), str(0)),
 ]
 )
 engine = PositionStateEngine(events=events, reserve_table=RESERVES)
 liquidations = events[events["event_type"] == "LiquidationCall"]
 oracle = _StubOracle({WETH: 5000.0, USDC: 1.0})

 report, _ = reconstruct_hf_at_trigger(engine, liquidations, oracle)

 row = report.iloc[0]
 assert row["block_number"] == 102
 assert row["health_factor"] is None # empty pre-liq state -> no debt
 assert not row["measurable"]


def test_reconstruct_hf_within_tolerance_band_is_not_mismatch:
 #: HF_TOLERANCE=0.01 -- a reconstructed HF of 1.005 is
 # inside the [1.0, 1.0 + HF_TOLERANCE) band, so this is NOT a mismatch
 # (pre-ADR-005, the exact-boundary reading would have flagged any HF
 # >= 1.0 as a mismatch).
 events = pd.DataFrame(
 [
 _core_event(100, 0, "Deposit", "u_band", WETH, str(1 * 10**18)),
 _core_event(101, 0, "Borrow", "u_band", USDC, str(100 * 10**6)),
 _liquidation_event(102, 0, "u_band", WETH, USDC, str(50 * 10**6), str(0)),
 ]
 )
 engine = PositionStateEngine(events=events, reserve_table=RESERVES)
 liquidations = events[events["event_type"] == "LiquidationCall"]
 # WETH liquidation_threshold = 0.86 (reserves.py); HF = price * 0.86 / 100
 # debt, so this price makes HF == 1.0 + HF_TOLERANCE / 2 (1.005 at the
 # current HF_TOLERANCE=0.01) -- inside the band.
 target_hf = 1.0 + HF_TOLERANCE / 2
 price = 100.0 * target_hf / 0.86
 oracle = _StubOracle({WETH: price, USDC: 1.0})

 report, _ = reconstruct_hf_at_trigger(engine, liquidations, oracle)

 row = report.iloc[0]
 assert row["measurable"]
 assert row["health_factor"] == pytest.approx(target_hf)
 assert not row["mismatch"]


def test_reconstruct_hf_outside_tolerance_band_is_mismatch:
 #: HF=1.02 clears 1.0 by more than HF_TOLERANCE=0.01, so
 # it's still flagged as a mismatch under the tolerance band.
 events = pd.DataFrame(
 [
 _core_event(100, 0, "Deposit", "u_outside", WETH, str(1 * 10**18)),
 _core_event(101, 0, "Borrow", "u_outside", USDC, str(100 * 10**6)),
 _liquidation_event(
 102, 0, "u_outside", WETH, USDC, str(50 * 10**6), str(0)
 ),
 ]
 )
 engine = PositionStateEngine(events=events, reserve_table=RESERVES)
 liquidations = events[events["event_type"] == "LiquidationCall"]
 # HF == 1.0 + 2 * HF_TOLERANCE (1.02 at the current HF_TOLERANCE=0.01)
 # outside the band.
 target_hf = 1.0 + 2 * HF_TOLERANCE
 price = 100.0 * target_hf / 0.86
 oracle = _StubOracle({WETH: price, USDC: 1.0})

 report, _ = reconstruct_hf_at_trigger(engine, liquidations, oracle)

 row = report.iloc[0]
 assert row["measurable"]
 assert row["health_factor"] == pytest.approx(target_hf)
 assert row["mismatch"]


def test_reconstruct_empty_liquidations_returns_empty:
 engine = PositionStateEngine(
 events=pd.DataFrame(columns=["event_type"]), reserve_table=RESERVES
 )
 report, position_by_key = reconstruct_hf_at_trigger(
 engine,
 pd.DataFrame(columns=["protocol", "user", "block_number", "block_timestamp"]),
 _StubOracle({}),
 )
 assert report.empty
 assert position_by_key == {}


# ---------------------------------------------------------------------------
# Same-block-earlier-activity, generalized beyond cascades
# ---------------------------------------------------------------------------

_TEST_RAY = 10**27
_ATOKEN_EVENT_COLUMNS = [
 "block_number",
 "log_index",
 "tx_hash",
 "reserve",
 "event_type",
 "address_1",
 "address_2",
 "value_raw",
 "index_raw",
]
_VDEBT_EVENT_COLUMNS = [
 "block_number",
 "log_index",
 "tx_hash",
 "reserve",
 "event_type",
 "user",
 "value_raw",
 "index_raw",
]
_SDEBT_EVENT_COLUMNS = [
 "block_number",
 "log_index",
 "block_timestamp",
 "tx_hash",
 "reserve",
 "event_type",
 "user",
 "amount_raw",
 "current_balance_raw",
 "avg_stable_rate_raw",
]


def _atoken_event(
 block_number, log_index, event_type, address_1, address_2, value, tx_hash
):
 return {
 "block_number": block_number,
 "log_index": log_index,
 "tx_hash": tx_hash,
 "reserve": WETH,
 "event_type": event_type,
 "address_1": address_1,
 "address_2": address_2,
 "value_raw": str(int(value * 10**18)),
 "index_raw": str(_TEST_RAY),
 }


def _variable_debt_event(block_number, log_index, event_type, user, value, tx_hash):
 return {
 "block_number": block_number,
 "log_index": log_index,
 "tx_hash": tx_hash,
 "reserve": USDC,
 "event_type": event_type,
 "user": user,
 "value_raw": str(int(value * 10**6)),
 "index_raw": str(_TEST_RAY),
 }


def _write_token_ledger(tmp_path, monkeypatch, atoken_rows=None, vdebt_rows=None):
 import cascadesignal.state.engine as engine_module

 out_dir = tmp_path / "token_ledger"
 out_dir.mkdir(parents=True, exist_ok=True)
 atoken_path = out_dir / "atoken_events.parquet"
 vdebt_path = out_dir / "variable_debt_events.parquet"
 sdebt_path = out_dir / "stable_debt_events.parquet"
 pd.DataFrame(atoken_rows or [], columns=_ATOKEN_EVENT_COLUMNS).to_parquet(
 atoken_path
 )
 pd.DataFrame(vdebt_rows or [], columns=_VDEBT_EVENT_COLUMNS).to_parquet(vdebt_path)
 pd.DataFrame([], columns=_SDEBT_EVENT_COLUMNS).to_parquet(sdebt_path)
 monkeypatch.setattr(engine_module, "_ATOKEN_EVENTS_PATH", atoken_path)
 monkeypatch.setattr(engine_module, "_VARIABLE_DEBT_EVENTS_PATH", vdebt_path)
 monkeypatch.setattr(engine_module, "_STABLE_DEBT_EVENTS_PATH", sdebt_path)


def test_reconstruct_sees_same_block_withdraw_from_a_different_tx(
 tmp_path, monkeypatch
):
 # Generalizes the cascade fix beyond "another LiquidationCall": a
 # Withdraw earlier in the SAME block as the trigger, in a DIFFERENT tx,
 # was invisible to the old block_number - 1-only cut (which excludes the
 # WHOLE trigger block) because there was no other *trigger* to detect it
 # via -- only `same_block_earlier_tx`'s direct ledger scan can.
 _write_token_ledger(
 tmp_path,
 monkeypatch,
 atoken_rows=[
 _atoken_event(100, 0, "Mint", "0xu1", None, value=10.0, tx_hash="0xmint"),
 # Withdraw earlier in block 200, a DIFFERENT tx than the
 # liquidation below.
 _atoken_event(
 200, 5, "Burn", "0xu1", "0xu1", value=2.0, tx_hash="0xwithdraw"
 ),
 ],
 vdebt_rows=[
 _variable_debt_event(
 100, 1, "Mint", "0xu1", value=100.0, tx_hash="0xborrow"
 ),
 ],
 )
 liquidations = pd.DataFrame(
 [_liquidation_event(200, 20, "0xu1", WETH, USDC, str(50 * 10**6), str(0))]
 )
 engine = PositionStateEngine(events=pd.DataFrame, reserve_table=RESERVES)

 _report, position_by_key = reconstruct_hf_at_trigger(
 engine, liquidations, _StubOracle({WETH: 2000.0, USDC: 1.0})
 )

 position = position_by_key[("0xu1", 200, 20)]
 weth_row = position[position["reserve"] == WETH].iloc[0]
 # 10 minted - 2 withdrawn earlier in the SAME block = 8, not 10 (which a
 # block_number - 1-only cut would have wrongly returned).
 assert weth_row["collateral_units"] == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# mismatch_summary
# ---------------------------------------------------------------------------


def test_mismatch_summary_groups_by_protocol_and_reports_overall:
 events = pd.DataFrame(
 [
 _core_event(
 100, 0, "Deposit", "u1", WETH, str(1 * 10**18), protocol="protocol_a"
 ),
 _core_event(
 101, 0, "Borrow", "u1", USDC, str(100 * 10**6), protocol="protocol_a"
 ),
 _liquidation_event(
 102, 0, "u1", WETH, USDC, str(50 * 10**6), str(0), protocol="protocol_a"
 ),
 _core_event(
 200, 0, "Deposit", "u2", WETH, str(1 * 10**18), protocol="protocol_b"
 ),
 _core_event(
 201, 0, "Borrow", "u2", USDC, str(100 * 10**6), protocol="protocol_b"
 ),
 _liquidation_event(
 202, 0, "u2", WETH, USDC, str(50 * 10**6), str(0), protocol="protocol_b"
 ),
 ]
 )
 engine = PositionStateEngine(events=events, reserve_table=RESERVES)
 liquidations = events[events["event_type"] == "LiquidationCall"]
 # u1: generous price -> mismatch. u2: low price (well below what the
 # remaining $50 debt needs, for any liquidation_threshold <= 1) -> matches.
 oracle_a = _StubOracle({WETH: 5000.0, USDC: 1.0})
 report_a, _ = reconstruct_hf_at_trigger(
 engine, liquidations[liquidations["protocol"] == "protocol_a"], oracle_a
 )
 oracle_b = _StubOracle({WETH: 20.0, USDC: 1.0})
 report_b, _ = reconstruct_hf_at_trigger(
 engine, liquidations[liquidations["protocol"] == "protocol_b"], oracle_b
 )
 report = pd.concat([report_a, report_b], ignore_index=True)

 summary = mismatch_summary(report)

 assert summary.loc["protocol_a", "n_mismatch"] == 1
 assert summary.loc["protocol_a", "mismatch_rate"] == pytest.approx(1.0)
 assert summary.loc["protocol_b", "n_mismatch"] == 0
 assert summary.loc["protocol_b", "mismatch_rate"] == pytest.approx(0.0)
 assert summary.loc["OVERALL", "n_measurable"] == 2
 assert summary.loc["OVERALL", "n_mismatch"] == 1
 assert summary.loc["OVERALL", "mismatch_rate"] == pytest.approx(0.5)


def test_mismatch_summary_empty_report:
 summary = mismatch_summary(
 pd.DataFrame(columns=["protocol", "measurable", "mismatch"])
 )
 assert summary.empty


# ---------------------------------------------------------------------------
# bucket_mismatch_causes
# ---------------------------------------------------------------------------


def _mismatched_weth_scenario(user: str, primary_price: float):
 events = pd.DataFrame(
 [
 _core_event(100, 0, "Deposit", user, WETH, str(1 * 10**18)),
 _core_event(101, 0, "Borrow", user, USDC, str(100 * 10**6)),
 _liquidation_event(102, 0, user, WETH, USDC, str(50 * 10**6), str(0)),
 ]
 )
 engine = PositionStateEngine(events=events, reserve_table=RESERVES)
 liquidations = events[events["event_type"] == "LiquidationCall"]
 oracle = _StubOracle({WETH: primary_price, USDC: 1.0})
 report, position_by_key = reconstruct_hf_at_trigger(engine, liquidations, oracle)
 assert report.iloc[0]["mismatch"] # sanity: scenario is actually a mismatch
 return engine, report, position_by_key


def test_bucket_causes_oracle_lag_when_chainlink_flips_verdict:
 engine, report, position_by_key = _mismatched_weth_scenario(
 "u1", primary_price=5000.0
 )
 # Remaining debt is $50 against 1 WETH -- a price this low can't cover it
 # under any liquidation_threshold <= 1, so it reliably flips the verdict.
 chainlink = _StubOracle({WETH: 30.0, USDC: 1.0})

 causes = bucket_mismatch_causes(report, position_by_key, engine, chainlink)

 assert causes.iloc[0] == "oracle_lag"


def test_bucket_causes_unexplained_when_chainlink_confirms_mismatch:
 engine, report, position_by_key = _mismatched_weth_scenario(
 "u1", primary_price=5000.0
 )
 chainlink = _StubOracle({WETH: 5000.0, USDC: 1.0}) # same price -> still HF >= 1

 causes = bucket_mismatch_causes(report, position_by_key, engine, chainlink)

 assert causes.iloc[0] == "unexplained"


def test_bucket_causes_no_chainlink_coverage:
 engine, report, position_by_key = _mismatched_weth_scenario(
 "u1", primary_price=5000.0
 )
 chainlink_missing_reserve = _StubOracle({USDC: 1.0}) # no WETH price at all

 causes = bucket_mismatch_causes(
 report, position_by_key, engine, chainlink_missing_reserve
 )

 assert causes.iloc[0] == "unexplained_no_chainlink_coverage"

 causes_no_oracle = bucket_mismatch_causes(report, position_by_key, engine, None)
 assert causes_no_oracle.iloc[0] == "unexplained_no_chainlink_coverage"


def test_bucket_causes_ignores_dust_reserve_missing_coverage:
 # A real, fully-priced WETH/USDC mismatch, plus 1 wei of LINK (~1e-18
 # units, far below health_factor._EPS) that chainlink_oracle has no
 # price for at all. The dust never affects compute_health_factor's
 # verdict either way (same _EPS gate), so this must still bucket as
 # `unexplained`, not `unexplained_no_chainlink_coverage` --
 # post-Lever-11c fix: a position's real 174-mismatch residual traced to
 # exactly this pattern (an irrelevant dust reserve predating its own
 # Chainlink history tripping the label on an otherwise-fully-covered
 # position).
 events = pd.DataFrame(
 [
 _core_event(99, 0, "Deposit", "u1", LINK, "1"), # 1 wei dust
 _core_event(100, 0, "Deposit", "u1", WETH, str(1 * 10**18)),
 _core_event(101, 0, "Borrow", "u1", USDC, str(100 * 10**6)),
 _liquidation_event(102, 0, "u1", WETH, USDC, str(50 * 10**6), str(0)),
 ]
 )
 engine = PositionStateEngine(events=events, reserve_table=RESERVES)
 liquidations = events[events["event_type"] == "LiquidationCall"]
 oracle = _StubOracle({WETH: 5000.0, USDC: 1.0, LINK: 10.0})
 report, position_by_key = reconstruct_hf_at_trigger(engine, liquidations, oracle)
 assert report.iloc[0]["mismatch"]

 chainlink_missing_dust_only = _StubOracle(
 {WETH: 5000.0, USDC: 1.0}
 ) # no LINK price
 causes = bucket_mismatch_causes(
 report, position_by_key, engine, chainlink_missing_dust_only
 )

 assert causes.iloc[0] == "unexplained"


def test_bucket_causes_param_drift_for_derisked_reserve:
 events = pd.DataFrame(
 [
 _core_event(100, 0, "Deposit", "u_yfi", YFI, str(1000 * 10**18)),
 _core_event(101, 0, "Borrow", "u_yfi", USDC, str(100 * 10**6)),
 _liquidation_event(102, 0, "u_yfi", YFI, USDC, str(50 * 10**6), str(0)),
 ]
 )
 engine = PositionStateEngine(events=events, reserve_table=RESERVES)
 liquidations = events[events["event_type"] == "LiquidationCall"]
 lt_yfi = RESERVES.set_index("address").loc[YFI, "liquidation_threshold"]
 assert lt_yfi > 0
 # Price chosen so weighted collateral comfortably clears the tiny remaining
 # debt even through YFI's de-risked threshold -- a guaranteed mismatch.
 price = (100.0 / lt_yfi) * 10
 oracle = _StubOracle({YFI: price, USDC: 1.0})
 report, position_by_key = reconstruct_hf_at_trigger(engine, liquidations, oracle)
 assert report.iloc[0]["mismatch"]
 assert not report.iloc[0]["historical_reliable"]

 # Even with a chainlink oracle available and agreeing on price, param
 # drift should be diagnosed first (short-circuits the oracle-lag check).
 chainlink = _StubOracle({YFI: price, USDC: 1.0})
 causes = bucket_mismatch_causes(report, position_by_key, engine, chainlink)

 assert causes.iloc[0] == "param_drift"


# ---------------------------------------------------------------------------
# attach_diagnostics
# ---------------------------------------------------------------------------


def test_attach_diagnostics_preserves_multi_leg_liquidations:
 # Two LiquidationCall rows in the same tx/block for the same user (e.g. a
 # multi-asset partial liquidation) should both surface the shared
 # trigger's HF/mismatch/cause, not get deduplicated away.
 events = pd.DataFrame(
 [
 _core_event(100, 0, "Deposit", "u1", WETH, str(1 * 10**18)),
 _core_event(101, 0, "Borrow", "u1", USDC, str(100 * 10**6)),
 _liquidation_event(102, 0, "u1", WETH, USDC, str(30 * 10**6), str(0)),
 _liquidation_event(102, 1, "u1", WETH, USDC, str(20 * 10**6), str(0)),
 ]
 )
 engine = PositionStateEngine(events=events, reserve_table=RESERVES)
 liquidations = events[events["event_type"] == "LiquidationCall"]
 oracle = _StubOracle({WETH: 5000.0, USDC: 1.0})
 report, position_by_key = reconstruct_hf_at_trigger(engine, liquidations, oracle)
 causes = bucket_mismatch_causes(report, position_by_key, engine, None)

 diagnostics = attach_diagnostics(report, liquidations, causes)

 assert len(diagnostics) == 2 # one row per raw liquidation event, not deduped
 assert diagnostics["mismatch"].all
 assert (diagnostics["cause"] == "unexplained_no_chainlink_coverage").all
 assert set(diagnostics["tx_hash"]) == {"0xtx102_0", "0xtx102_1"}


# ---------------------------------------------------------------------------
# Real-data T2 gate ( actual enforcement mechanism)
# ---------------------------------------------------------------------------

pytestmark_real_data = pytest.mark.skipif(
 not _HAS_REAL_DATA, reason="requires ingested data/raw/aave_v2 (Git LFS pull)"
)


@pytestmark_real_data
@pytest.mark.t2_gate
def test_t2_gate_enforces_two_percent_mismatch:
 """The actual, now PASSING under ADR-005's
 tolerance band (1.093% tolerance-band rate vs. the 2% bar) -- the first
 time in this ticket's history. Still excluded from the default
 `pytest`/pre-commit/CI run via the `t2_gate` marker (see pyproject.toml's
 `addopts`; runtime is ~1 min against the real LFS-pulled data, not worth
 paying on every unrelated commit) -- run explicitly with
 `pytest -m t2_gate` to check current gate status. Run
 `scripts/analysis/t2_mismatch_report.py` for the full, persisted
 per-event diagnostics artifact (prints both the tolerance-band and
 exact-boundary rates, per ADR-005); this test also prints both inline
 (via the assertion message, shown on failure) so a regression is
 self-diagnosing without needing to dig up a separate file.

 Live-oracle fallback (Lever 12): applies
 `apply_live_oracle_fallback` unconditionally, same as every prior lever
 became unconditional once landed -- not an opt-in flag here. Safe to
 apply with no network access in the common case: the relevant
 (address, block) pairs for the current `unexplained` population are
 already cached in the tracked, LFS-pulled
 `data/raw/aave_oracle_live/chain=1/asset_price_cache.parquet` (98.0%
 coverage measured against the full population, not a sample), so this
 resolves from disk, not a live `eth_call`, for anyone with the data lake
 pulled. A future session's *new* mismatches (past this cache's coverage)
 simply go uncorrected rather than erroring -- see
 `LiveAaveOracleFallback`'s docstring.
 """
 from cascadesignal.state.prices import (
 LiveAaveOracleFallback,
 PreferEthNumeraireOracle,
 )
 from cascadesignal.state.reserve_config_history import ReserveConfigHistory

 events = load_events(data_dir=DATA_DIR, protocol="aave_v2")
 liquidations = events[events["event_type"] == "LiquidationCall"]
 engine = PositionStateEngine(events=events, reserve_table=RESERVES)
 #: native per-position ETH-numeraire price, falling back to
 # Chainlink-blended USD (Lever 2) for any position with a leg outside ETH
 # feed coverage -- the feed Aave read, in the numeraire Aave read it in.
 price_oracle = PreferEthNumeraireOracle
 #: point-in-time liquidation thresholds.
 try:
 config_history: ReserveConfigHistory | None = ReserveConfigHistory
 except FileNotFoundError:
 config_history = None

 report, position_by_key = reconstruct_hf_at_trigger(
 engine, liquidations, price_oracle, config_history
 )

 #: pass the SAME oracle used for the primary
 # reconstruction, not a separate, narrower `ChainlinkPriceOracle` -- see
 # `bucket_mismatch_causes`'s docstring. Matters only for the failure
 # message's cause breakdown below (not the pass/fail assertion itself).
 causes = bucket_mismatch_causes(
 report, position_by_key, engine, price_oracle, config_history
 )

 #: see docstring above.
 live_oracle = LiveAaveOracleFallback
 report = apply_live_oracle_fallback(
 report, causes, position_by_key, engine, live_oracle, config_history
 )
 causes = causes.where(report["mismatch"])

 summary = mismatch_summary(report)
 mismatched = report.assign(cause=causes)[report["mismatch"]]
 cause_counts = mismatched["cause"].value_counts

 overall_rate = summary.loc["OVERALL", "mismatch_rate"]
 overall_rate_exact = summary.loc["OVERALL", "mismatch_rate_exact"]
 assert overall_rate <= MISMATCH_THRESHOLD, (
 f"T2 gate FAILED: {overall_rate:.2%} of measurable liquidations reconstruct "
 f"to HF >= 1.0 + HF_TOLERANCE ({HF_TOLERANCE:.0%}) at trigger (threshold "
 f"{MISMATCH_THRESHOLD:.0%}); exact-boundary rate (HF >= 1.0) is "
 f"{overall_rate_exact:.2%}. Per ADR-005, this gate previously passed at "
 "1.093% tolerance-band -- a regression here means either a real "
 "reconstruction bug or that ADR-005's frozen HF_TOLERANCE needs a "
 "superseding ADR, not a silent threshold bump -- "
 "run scripts/analysis/t2_mismatch_report.py for the full per-event "
 f"diagnostics artifact.\n\nPer-protocol + overall:\n{summary.to_string}"
 f"\n\nMismatch cause breakdown:\n{cause_counts.to_string}"
 )
