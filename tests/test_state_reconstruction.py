"""Unit tests for the position-state reconstruction engine (CAS-13/CAS-47).

These are engine-correctness tests (event replay, snapshot queries, the HF
formula) on synthetic fixtures. The full T2 correctness gate (every observed
Aave v2 liquidation reconstructs to HF < 1 at its trigger block, > 2%
mismatch blocks the pipeline) is CAS-28, a separate ticket that depends on
this engine — not implemented here.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import cascadesignal.state.engine as engine_module
from cascadesignal.state.engine import PositionStateEngine, build_ledger, load_events
from cascadesignal.state.health_factor import compute_health_factor
from cascadesignal.state.interest_index import (
    InterestIndexOracle,
    calculate_compounded_interest,
)
from cascadesignal.state.reserves import reserve_table

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
DATA_DIR = Path("data/raw")


def _has_real_parquet(directory: Path) -> bool:
    """True only if `directory` holds a real parquet file (PAR1 magic bytes),
    not a Git LFS pointer -- mirrors tests/test_cascade_labeler.py's guard."""
    if not directory.exists():
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


def _core_event(
    block_number: int,
    log_index: int,
    event_type: str,
    user: str,
    reserve: str,
    amount_raw: str,
) -> dict:
    return {
        "chain_id": 1,
        "block_number": block_number,
        "log_index": log_index,
        "tx_hash": f"0xtx{block_number}_{log_index}",
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
    block_number: int,
    log_index: int,
    user: str,
    collateral_asset: str,
    debt_asset: str,
    debt_repaid_raw: str,
    collateral_seized_raw: str,
) -> dict:
    return {
        "chain_id": 1,
        "block_number": block_number,
        "log_index": log_index,
        "tx_hash": f"0xtx{block_number}_{log_index}",
        "protocol": "aave_v2",
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


RESERVES = reserve_table()


# ---------------------------------------------------------------------------
# Ledger construction / replay
# ---------------------------------------------------------------------------


def test_deposit_borrow_repay_withdraw_nets_correctly():
    events = pd.DataFrame(
        [
            _core_event(100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18)),
            _core_event(101, 0, "Borrow", "0xu1", USDC, str(1000 * 10**6)),
            _core_event(102, 0, "Repay", "0xu1", USDC, str(400 * 10**6)),
            _core_event(103, 0, "Withdraw", "0xu1", WETH, str(1 * 10**18)),
        ]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)
    position = engine.account_snapshot("0xu1", block_number=103)

    weth_row = position[position["reserve"] == WETH].iloc[0]
    usdc_row = position[position["reserve"] == USDC].iloc[0]
    assert weth_row["collateral_units"] == pytest.approx(1.0)
    assert usdc_row["debt_units"] == pytest.approx(600.0)


def test_positions_at_respects_block_cutoff():
    events = pd.DataFrame(
        [
            _core_event(100, 0, "Deposit", "0xu1", WETH, str(1 * 10**18)),
            _core_event(200, 0, "Deposit", "0xu1", WETH, str(1 * 10**18)),
        ]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)

    early = engine.account_snapshot("0xu1", block_number=150)
    late = engine.account_snapshot("0xu1", block_number=200)

    assert early.iloc[0]["collateral_units"] == pytest.approx(1.0)
    assert late.iloc[0]["collateral_units"] == pytest.approx(2.0)


def test_liquidation_debits_collateral_and_debt():
    events = pd.DataFrame(
        [
            _core_event(100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18)),
            _core_event(101, 0, "Borrow", "0xu1", USDC, str(1000 * 10**6)),
            _liquidation_event(
                102, 0, "0xu1", WETH, USDC, str(500 * 10**6), str(1 * 10**18)
            ),
        ]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)
    position = engine.account_snapshot("0xu1", block_number=102)

    weth_row = position[position["reserve"] == WETH].iloc[0]
    usdc_row = position[position["reserve"] == USDC].iloc[0]
    assert weth_row["collateral_units"] == pytest.approx(1.0)  # 2 - 1 seized
    assert usdc_row["debt_units"] == pytest.approx(500.0)  # 1000 - 500 repaid


def test_position_at_log_index_sees_earlier_same_block_liquidation():
    # A same-block liquidation cascade (CAS-28 H4): two LiquidationCall
    # events for the same user in block 102, log_index 0 then 1. The second
    # liquidation's true pre-state must include the first's seizure/repay --
    # EVM execution is sequential even within one block.
    events = pd.DataFrame(
        [
            _core_event(100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18)),
            _core_event(101, 0, "Borrow", "0xu1", USDC, str(200 * 10**6)),
            _liquidation_event(
                102, 0, "0xu1", WETH, USDC, str(100 * 10**6), str(1 * 10**18)
            ),
            _liquidation_event(102, 1, "0xu1", WETH, USDC, str(50 * 10**6), str(0)),
        ]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)

    before_first = engine.position_at_log_index("0xu1", 102, 0)
    before_second = engine.position_at_log_index("0xu1", 102, 1)

    # Just before the first liquidation: unaffected, same as block-101 state.
    assert before_first[before_first["reserve"] == WETH].iloc[0][
        "collateral_units"
    ] == pytest.approx(2.0)
    assert before_first[before_first["reserve"] == USDC].iloc[0][
        "debt_units"
    ] == pytest.approx(200.0)

    # Just before the second liquidation: reflects the first's seizure (1
    # WETH) and repay (100 USDC), NOT the original block-101 state.
    assert before_second[before_second["reserve"] == WETH].iloc[0][
        "collateral_units"
    ] == pytest.approx(1.0)
    assert before_second[before_second["reserve"] == USDC].iloc[0][
        "debt_units"
    ] == pytest.approx(100.0)


def test_positions_drop_netted_zero_rows():
    events = pd.DataFrame(
        [
            _core_event(100, 0, "Deposit", "0xu1", WETH, str(1 * 10**18)),
            _core_event(101, 0, "Withdraw", "0xu1", WETH, str(1 * 10**18)),
        ]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)
    position = engine.account_snapshot("0xu1", block_number=101)
    assert position.empty


def test_build_ledger_empty_events_returns_empty_frame():
    ledger = build_ledger(pd.DataFrame(columns=["event_type"]), RESERVES)
    assert ledger.empty


# ---------------------------------------------------------------------------
# onBehalfOf / Withdraw correction (CAS-28) -- Aave v2 Borrow's and Deposit's
# `user` field is msg.sender, not the real position holder;
# gateway/adapter-routed events get re-attributed via corrections tables.
# `Borrow` is always safe to correct on its own. `Deposit` is only corrected
# when the symmetric `Withdraw` correction table also exists -- applying
# `Deposit` alone creates phantom collateral (see engine.py's module
# docstring). See scripts/onchain/fix_gateway_onbehalfof.py and
# scripts/onchain/fix_gateway_withdraw.py.
# ---------------------------------------------------------------------------

GATEWAY = "0xcc9a0b7c43dc2a5f023bb9b738e45b0ef6b06e04"
REAL_USER = "0x56618ca46b82a1309f15aa3ec5dfc894756dc069"


def test_apply_onbehalfof_correction_reattributes_matched_borrow_rows(
    tmp_path, monkeypatch
):
    corrections_path = tmp_path / "onbehalfof.parquet"
    pd.DataFrame(
        [
            {
                "tx_hash": "0xtx100_0",
                "log_index": 0,
                "event_type": "Borrow",
                "onbehalfof": REAL_USER,
            }
        ]
    ).to_parquet(corrections_path)
    monkeypatch.setattr(engine_module, "_ONBEHALFOF_CORRECTIONS_PATH", corrections_path)
    monkeypatch.setattr(
        engine_module,
        "_WITHDRAW_CORRECTIONS_PATH",
        tmp_path / "missing_withdraw.parquet",
    )

    events = pd.DataFrame(
        [_core_event(100, 0, "Borrow", GATEWAY, WETH, str(1 * 10**18))]
    )
    corrected = engine_module._apply_onbehalfof_correction(events)
    assert corrected.iloc[0]["user"] == REAL_USER


def test_apply_onbehalfof_correction_does_not_reattribute_deposit_rows_without_withdraw_fix(
    tmp_path, monkeypatch
):
    # Deposit's counterpart, Withdraw, isn't correctable via onBehalfOf (no
    # such field on-chain) -- it needs the separate aToken Transfer-event
    # correlation table. Without that table present, applying Deposit alone
    # would create phantom collateral, so it must stay a no-op even when the
    # onBehalfOf corrections table has a Deposit row for this key.
    corrections_path = tmp_path / "onbehalfof.parquet"
    pd.DataFrame(
        [
            {
                "tx_hash": "0xtx100_0",
                "log_index": 0,
                "event_type": "Deposit",
                "onbehalfof": REAL_USER,
            }
        ]
    ).to_parquet(corrections_path)
    monkeypatch.setattr(engine_module, "_ONBEHALFOF_CORRECTIONS_PATH", corrections_path)
    monkeypatch.setattr(
        engine_module,
        "_WITHDRAW_CORRECTIONS_PATH",
        tmp_path / "missing_withdraw.parquet",
    )

    events = pd.DataFrame(
        [_core_event(100, 0, "Deposit", GATEWAY, WETH, str(1 * 10**18))]
    )
    corrected = engine_module._apply_onbehalfof_correction(events)
    assert corrected.iloc[0]["user"] == GATEWAY  # Deposit not corrected


def test_apply_onbehalfof_correction_reattributes_deposit_when_withdraw_fix_present(
    tmp_path, monkeypatch
):
    # Once the symmetric Withdraw correction table exists (regardless of its
    # content for *this* key -- its mere presence signals the paired fix has
    # been pulled), Deposit rows become safe to re-attribute too.
    corrections_path = tmp_path / "onbehalfof.parquet"
    pd.DataFrame(
        [
            {
                "tx_hash": "0xtx100_0",
                "log_index": 0,
                "event_type": "Deposit",
                "onbehalfof": REAL_USER,
            }
        ]
    ).to_parquet(corrections_path)
    monkeypatch.setattr(engine_module, "_ONBEHALFOF_CORRECTIONS_PATH", corrections_path)

    withdraw_path = tmp_path / "withdraw.parquet"
    pd.DataFrame(
        [{"tx_hash": "0xtx_unrelated", "log_index": 9, "real_user": REAL_USER}]
    ).to_parquet(withdraw_path)
    monkeypatch.setattr(engine_module, "_WITHDRAW_CORRECTIONS_PATH", withdraw_path)

    events = pd.DataFrame(
        [_core_event(100, 0, "Deposit", GATEWAY, WETH, str(1 * 10**18))]
    )
    corrected = engine_module._apply_onbehalfof_correction(events)
    assert corrected.iloc[0]["user"] == REAL_USER


def test_apply_onbehalfof_correction_leaves_unmatched_rows_untouched(
    tmp_path, monkeypatch
):
    corrections_path = tmp_path / "onbehalfof.parquet"
    pd.DataFrame(
        [
            {
                "tx_hash": "0xsome_other_tx",
                "log_index": 5,
                "event_type": "Borrow",
                "onbehalfof": REAL_USER,
            }
        ]
    ).to_parquet(corrections_path)
    monkeypatch.setattr(engine_module, "_ONBEHALFOF_CORRECTIONS_PATH", corrections_path)
    monkeypatch.setattr(
        engine_module,
        "_WITHDRAW_CORRECTIONS_PATH",
        tmp_path / "missing_withdraw.parquet",
    )

    events = pd.DataFrame(
        [_core_event(100, 0, "Borrow", GATEWAY, WETH, str(1 * 10**18))]
    )
    corrected = engine_module._apply_onbehalfof_correction(events)
    assert corrected.iloc[0]["user"] == GATEWAY  # no match -> unchanged


def test_apply_onbehalfof_correction_is_noop_without_corrections_file(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        engine_module, "_ONBEHALFOF_CORRECTIONS_PATH", tmp_path / "missing.parquet"
    )
    monkeypatch.setattr(
        engine_module,
        "_WITHDRAW_CORRECTIONS_PATH",
        tmp_path / "missing_withdraw.parquet",
    )
    events = pd.DataFrame(
        [_core_event(100, 0, "Borrow", GATEWAY, WETH, str(1 * 10**18))]
    )
    corrected = engine_module._apply_onbehalfof_correction(events)
    pd.testing.assert_frame_equal(corrected, events)


# ---------------------------------------------------------------------------
# Withdraw correction (CAS-28) -- see scripts/onchain/fix_gateway_withdraw.py
# ---------------------------------------------------------------------------


def test_apply_withdraw_correction_reattributes_matched_rows(tmp_path, monkeypatch):
    withdraw_path = tmp_path / "withdraw.parquet"
    pd.DataFrame(
        [{"tx_hash": "0xtx100_0", "log_index": 0, "real_user": REAL_USER}]
    ).to_parquet(withdraw_path)
    monkeypatch.setattr(engine_module, "_WITHDRAW_CORRECTIONS_PATH", withdraw_path)

    events = pd.DataFrame(
        [_core_event(100, 0, "Withdraw", GATEWAY, WETH, str(1 * 10**18))]
    )
    corrected = engine_module._apply_withdraw_correction(events)
    assert corrected.iloc[0]["user"] == REAL_USER


def test_apply_withdraw_correction_leaves_unmatched_rows_untouched(
    tmp_path, monkeypatch
):
    withdraw_path = tmp_path / "withdraw.parquet"
    pd.DataFrame(
        [{"tx_hash": "0xsome_other_tx", "log_index": 5, "real_user": REAL_USER}]
    ).to_parquet(withdraw_path)
    monkeypatch.setattr(engine_module, "_WITHDRAW_CORRECTIONS_PATH", withdraw_path)

    events = pd.DataFrame(
        [_core_event(100, 0, "Withdraw", GATEWAY, WETH, str(1 * 10**18))]
    )
    corrected = engine_module._apply_withdraw_correction(events)
    assert corrected.iloc[0]["user"] == GATEWAY  # no match -> unchanged


def test_apply_withdraw_correction_is_noop_without_corrections_file(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        engine_module, "_WITHDRAW_CORRECTIONS_PATH", tmp_path / "missing.parquet"
    )
    events = pd.DataFrame(
        [_core_event(100, 0, "Withdraw", GATEWAY, WETH, str(1 * 10**18))]
    )
    corrected = engine_module._apply_withdraw_correction(events)
    pd.testing.assert_frame_equal(corrected, events)


def test_load_events_applies_deposit_and_withdraw_corrections_together(
    tmp_path, monkeypatch
):
    onbehalfof_path = tmp_path / "onbehalfof.parquet"
    pd.DataFrame(
        [
            {
                "tx_hash": "0xtx100_0",
                "log_index": 0,
                "event_type": "Deposit",
                "onbehalfof": REAL_USER,
            }
        ]
    ).to_parquet(onbehalfof_path)
    monkeypatch.setattr(engine_module, "_ONBEHALFOF_CORRECTIONS_PATH", onbehalfof_path)

    withdraw_path = tmp_path / "withdraw.parquet"
    pd.DataFrame(
        [{"tx_hash": "0xtx101_0", "log_index": 0, "real_user": REAL_USER}]
    ).to_parquet(withdraw_path)
    monkeypatch.setattr(engine_module, "_WITHDRAW_CORRECTIONS_PATH", withdraw_path)

    data_dir = tmp_path / "data" / "raw"
    out_dir = data_dir / "aave_v2" / "chain=1"
    out_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            _core_event(100, 0, "Deposit", GATEWAY, WETH, str(1 * 10**18)),
            _core_event(101, 0, "Withdraw", GATEWAY, WETH, str(1 * 10**18)),
        ]
    ).to_parquet(out_dir / "events.parquet")

    events = load_events(data_dir=data_dir, protocol="aave_v2")
    assert (events["user"] == REAL_USER).all()


def test_load_events_applies_correction_only_for_aave_v2(tmp_path, monkeypatch):
    corrections_path = tmp_path / "onbehalfof.parquet"
    pd.DataFrame(
        [
            {
                "tx_hash": "0xtx100_0",
                "log_index": 0,
                "event_type": "Borrow",
                "onbehalfof": REAL_USER,
            }
        ]
    ).to_parquet(corrections_path)
    monkeypatch.setattr(engine_module, "_ONBEHALFOF_CORRECTIONS_PATH", corrections_path)

    data_dir = tmp_path / "data" / "raw"
    for protocol in ("aave_v2", "aave_v3"):
        out_dir = data_dir / protocol / "chain=1"
        out_dir.mkdir(parents=True)
        pd.DataFrame(
            [_core_event(100, 0, "Borrow", GATEWAY, WETH, str(1 * 10**18))]
        ).to_parquet(out_dir / "events.parquet")

    aave_v2_events = load_events(data_dir=data_dir, protocol="aave_v2")
    aave_v3_events = load_events(data_dir=data_dir, protocol="aave_v3")

    assert aave_v2_events.iloc[0]["user"] == REAL_USER
    assert aave_v3_events.iloc[0]["user"] == GATEWAY  # correction is v2-only


# ---------------------------------------------------------------------------
# Health factor formula
# ---------------------------------------------------------------------------


def test_compute_health_factor_matches_formula():
    position = pd.DataFrame(
        [
            {"reserve": WETH, "collateral_units": 2.0, "debt_units": 0.0},
            {"reserve": USDC, "collateral_units": 0.0, "debt_units": 1000.0},
        ]
    )
    prices = {WETH: 2000.0, USDC: 1.0}
    result = compute_health_factor(position, prices, RESERVES)

    weth_lt = RESERVES.set_index("address").loc[WETH, "liquidation_threshold"]
    expected = (2.0 * 2000.0 * weth_lt) / 1000.0
    assert result.health_factor == pytest.approx(expected)
    assert result.fully_covered is True


def test_compute_health_factor_no_debt_is_none():
    position = pd.DataFrame(
        [{"reserve": WETH, "collateral_units": 2.0, "debt_units": 0.0}]
    )
    result = compute_health_factor(position, {WETH: 2000.0}, RESERVES)
    assert result.health_factor is None


def test_compute_health_factor_flags_missing_price_as_partial_coverage():
    position = pd.DataFrame(
        [
            {"reserve": WETH, "collateral_units": 2.0, "debt_units": 0.0},
            {"reserve": USDC, "collateral_units": 0.0, "debt_units": 1000.0},
        ]
    )
    # No price for USDC (the debt asset) -> can't be ground truth.
    result = compute_health_factor(position, {WETH: 2000.0}, RESERVES)
    assert result.fully_covered is False


def test_compute_health_factor_flags_unverified_threshold_as_partial_coverage():
    unmapped_reserve = "0xdeadbeef00000000000000000000000000000000"
    position = pd.DataFrame(
        [{"reserve": unmapped_reserve, "collateral_units": 1.0, "debt_units": 0.0}]
    )
    result = compute_health_factor(position, {unmapped_reserve: 1.0}, RESERVES)
    assert result.fully_covered is False


def test_compute_health_factor_historical_reliable_true_for_major_reserve():
    position = pd.DataFrame(
        [
            {"reserve": WETH, "collateral_units": 2.0, "debt_units": 0.0},
            {"reserve": USDC, "collateral_units": 0.0, "debt_units": 1000.0},
        ]
    )
    result = compute_health_factor(position, {WETH: 2000.0, USDC: 1.0}, RESERVES)
    assert result.historical_reliable is True


def test_compute_health_factor_historical_reliable_false_for_derisked_reserve():
    # YFI's current on-chain liquidation_threshold is ~0.05% -- de-risked
    # ahead of Aave v2's freeze, not representative of 2021-2022 collateral
    # value. See reserves.py's module docstring.
    yfi = "0x0bc529c00c6401aef6d220be8c6ea1667f6ad93e"
    position = pd.DataFrame(
        [
            {"reserve": yfi, "collateral_units": 5.0, "debt_units": 0.0},
            {"reserve": USDC, "collateral_units": 0.0, "debt_units": 1000.0},
        ]
    )
    result = compute_health_factor(position, {yfi: 5000.0, USDC: 1.0}, RESERVES)
    assert result.historical_reliable is False


# ---------------------------------------------------------------------------
# Point-in-time liquidation thresholds (thresholds_override, Lever 3, CAS-28)
# ---------------------------------------------------------------------------


def test_thresholds_override_replaces_frozen_threshold():
    position = pd.DataFrame(
        [
            {"reserve": WETH, "collateral_units": 2.0, "debt_units": 0.0},
            {"reserve": USDC, "collateral_units": 0.0, "debt_units": 1000.0},
        ]
    )
    prices = {WETH: 2000.0, USDC: 1.0}
    # Override WETH's threshold to a historical 0.70 instead of the frozen value.
    result = compute_health_factor(
        position, prices, RESERVES, thresholds_override={WETH: 0.70}
    )
    assert result.health_factor == pytest.approx((2.0 * 2000.0 * 0.70) / 1000.0)


def test_thresholds_override_falls_back_to_frozen_for_uncovered_reserve():
    # Override covers WETH but not USDC-collateral -> USDC uses its frozen LT.
    position = pd.DataFrame(
        [
            {"reserve": WETH, "collateral_units": 1.0, "debt_units": 0.0},
            {"reserve": USDC, "collateral_units": 1000.0, "debt_units": 0.0},
            {"reserve": WETH, "collateral_units": 0.0, "debt_units": 500.0},
        ]
    )
    frozen_usdc_lt = RESERVES.set_index("address").loc[USDC, "liquidation_threshold"]
    prices = {WETH: 2000.0, USDC: 1.0}
    result = compute_health_factor(
        position, prices, RESERVES, thresholds_override={WETH: 0.50}
    )
    expected = (1.0 * 2000.0 * 0.50 + 1000.0 * 1.0 * frozen_usdc_lt) / (500.0 * 2000.0)
    assert result.health_factor == pytest.approx(expected)


def test_thresholds_override_makes_derisked_reserve_historically_reliable():
    # A de-risked reserve (YFI) becomes historical_reliable once we supply its
    # real point-in-time threshold via the override.
    yfi = "0x0bc529c00c6401aef6d220be8c6ea1667f6ad93e"
    position = pd.DataFrame(
        [
            {"reserve": yfi, "collateral_units": 5.0, "debt_units": 0.0},
            {"reserve": USDC, "collateral_units": 0.0, "debt_units": 1000.0},
        ]
    )
    result = compute_health_factor(
        position, {yfi: 5000.0, USDC: 1.0}, RESERVES, thresholds_override={yfi: 0.65}
    )
    assert result.historical_reliable is True


# ---------------------------------------------------------------------------
# Collateral-toggle flag (Track B, CAS-28)
# ---------------------------------------------------------------------------


def test_compute_health_factor_excludes_disabled_collateral():
    position = pd.DataFrame(
        [
            {
                "reserve": WETH,
                "collateral_units": 2.0,
                "debt_units": 0.0,
                "collateral_enabled": False,
            },
            {
                "reserve": USDC,
                "collateral_units": 0.0,
                "debt_units": 1000.0,
                "collateral_enabled": True,
            },
        ]
    )
    result = compute_health_factor(position, {WETH: 2000.0, USDC: 1.0}, RESERVES)
    # WETH deposited but not enabled as collateral -> contributes nothing,
    # despite a real, positive balance and a known price/threshold.
    assert result.weighted_collateral_usd == pytest.approx(0.0)
    assert result.health_factor == pytest.approx(0.0)


def test_compute_health_factor_disabled_collateral_does_not_need_coverage():
    # A disabled reserve with no threshold/price at all still shouldn't mark
    # the position as partially-covered -- it's excluded before coverage is
    # even checked.
    unmapped_reserve = "0xdeadbeef00000000000000000000000000000000"
    position = pd.DataFrame(
        [
            {
                "reserve": unmapped_reserve,
                "collateral_units": 1.0,
                "debt_units": 0.0,
                "collateral_enabled": False,
            }
        ]
    )
    result = compute_health_factor(position, {}, RESERVES)
    assert result.fully_covered is True


def test_compute_health_factor_defaults_to_enabled_without_column():
    # Backward compatibility: callers that don't attach `collateral_enabled`
    # (e.g. hand-built test/analysis positions) get the original
    # unconditional behavior.
    position = pd.DataFrame(
        [{"reserve": WETH, "collateral_units": 2.0, "debt_units": 0.0}]
    )
    result = compute_health_factor(position, {WETH: 2000.0}, RESERVES)
    weth_lt = RESERVES.set_index("address").loc[WETH, "liquidation_threshold"]
    assert result.weighted_collateral_usd == pytest.approx(2.0 * 2000.0 * weth_lt)


def test_attach_collateral_enabled_is_noop_without_corrections_file(monkeypatch):
    monkeypatch.setattr(
        engine_module, "_COLLATERAL_TOGGLE_CORRECTIONS_PATH", Path("/nonexistent")
    )
    events = pd.DataFrame(
        [_core_event(100, 0, "Deposit", "0xu1", WETH, str(1 * 10**18))]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)
    position = engine.account_snapshot("0xu1", block_number=100)
    assert position.iloc[0]["collateral_enabled"] == True  # noqa: E712


def test_positions_at_reflects_latest_toggle_state_at_block(tmp_path, monkeypatch):
    toggle_path = tmp_path / "collateral_toggle.parquet"
    pd.DataFrame(
        [
            {
                "user": "0xu1",
                "reserve": WETH,
                "block_number": 150,
                "log_index": 0,
                "enabled": False,
            }
        ]
    ).to_parquet(toggle_path)
    monkeypatch.setattr(
        engine_module, "_COLLATERAL_TOGGLE_CORRECTIONS_PATH", toggle_path
    )

    events = pd.DataFrame(
        [_core_event(100, 0, "Deposit", "0xu1", WETH, str(1 * 10**18))]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)

    before_toggle = engine.account_snapshot("0xu1", block_number=120)
    after_toggle = engine.account_snapshot("0xu1", block_number=200)

    assert before_toggle.iloc[0]["collateral_enabled"] == True  # noqa: E712
    assert after_toggle.iloc[0]["collateral_enabled"] == False  # noqa: E712
    # The toggle only affects HF eligibility, not the underlying balance.
    assert after_toggle.iloc[0]["collateral_units"] == pytest.approx(1.0)


def test_positions_at_many_reflects_latest_toggle_state_at_block(tmp_path, monkeypatch):
    toggle_path = tmp_path / "collateral_toggle.parquet"
    pd.DataFrame(
        [
            {
                "user": "0xu1",
                "reserve": WETH,
                "block_number": 150,
                "log_index": 0,
                "enabled": False,
            }
        ]
    ).to_parquet(toggle_path)
    monkeypatch.setattr(
        engine_module, "_COLLATERAL_TOGGLE_CORRECTIONS_PATH", toggle_path
    )

    events = pd.DataFrame(
        [_core_event(100, 0, "Deposit", "0xu1", WETH, str(1 * 10**18))]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)
    keys = pd.DataFrame(
        [{"user": "0xu1", "block_number": 120}, {"user": "0xu1", "block_number": 200}]
    )
    positions = engine.positions_at_many(keys)

    early = positions[positions["block_number"] == 120].iloc[0]
    late = positions[positions["block_number"] == 200].iloc[0]
    assert early["collateral_enabled"] == True  # noqa: E712
    assert late["collateral_enabled"] == False  # noqa: E712


# ---------------------------------------------------------------------------
# aToken-transfer correction (Track C, CAS-28)
# ---------------------------------------------------------------------------


def test_atoken_transfer_correction_is_noop_without_corrections_file(monkeypatch):
    monkeypatch.setattr(
        engine_module, "_ATOKEN_TRANSFER_CORRECTIONS_PATH", Path("/nonexistent")
    )
    events = pd.DataFrame(
        [_core_event(100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18))]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)
    position = engine.account_snapshot("0xu1", block_number=100)
    assert position.iloc[0]["collateral_units"] == pytest.approx(2.0)


def test_atoken_transfer_correction_debits_sender_credits_receiver(
    tmp_path, monkeypatch
):
    corrections_path = tmp_path / "atoken_transfer.parquet"
    pd.DataFrame(
        [
            {
                "block_number": 150,
                "log_index": 0,
                "tx_hash": "0xnaked_transfer",
                "reserve": WETH,
                "from_user": "0xu1",
                "to_user": "0xu2",
                "value_raw": str(1 * 10**18),
            }
        ]
    ).to_parquet(corrections_path)
    monkeypatch.setattr(
        engine_module, "_ATOKEN_TRANSFER_CORRECTIONS_PATH", corrections_path
    )

    events = pd.DataFrame(
        [_core_event(100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18))]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)

    sender = engine.account_snapshot("0xu1", block_number=200)
    receiver = engine.account_snapshot("0xu2", block_number=200)
    assert sender.iloc[0]["collateral_units"] == pytest.approx(1.0)  # 2 - 1 naked
    assert receiver.iloc[0]["collateral_units"] == pytest.approx(1.0)


def test_atoken_transfer_correction_excludes_same_tx_as_liquidation(
    tmp_path, monkeypatch
):
    # A liquidator opting to `receiveAToken` seizes collateral via this exact
    # same aToken-Transfer mechanism, inside the LiquidationCall tx itself --
    # already accounted for by build_ledger's LiquidationCall branch, so the
    # correction must not double-debit it (see engine.py's module docstring).
    corrections_path = tmp_path / "atoken_transfer.parquet"
    pd.DataFrame(
        [
            {
                "block_number": 102,
                "log_index": 1,
                "tx_hash": "0xtx102_0",  # matches the liquidation event below
                "reserve": WETH,
                "from_user": "0xu1",
                "to_user": "0xliquidator",
                "value_raw": str(1 * 10**18),
            }
        ]
    ).to_parquet(corrections_path)
    monkeypatch.setattr(
        engine_module, "_ATOKEN_TRANSFER_CORRECTIONS_PATH", corrections_path
    )

    events = pd.DataFrame(
        [
            _core_event(100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18)),
            _core_event(101, 0, "Borrow", "0xu1", USDC, str(1000 * 10**6)),
            _liquidation_event(
                102, 0, "0xu1", WETH, USDC, str(500 * 10**6), str(1 * 10**18)
            ),
        ]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)
    position = engine.account_snapshot("0xu1", block_number=102)

    weth_row = position[position["reserve"] == WETH].iloc[0]
    # 2 - 1 seized by the liquidation; the correction's matching-tx_hash
    # transfer must NOT apply a second debit on top.
    assert weth_row["collateral_units"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Interest-index accrual scaling (Track E, CAS-28)
# ---------------------------------------------------------------------------


def _fake_interest_index_oracle(
    tmp_path: Path, rows: list[dict]
) -> InterestIndexOracle:
    """Build a real `InterestIndexOracle` from a controlled, tiny parquet
    (rather than the real multi-million-row pull) so Track E's index-scaling
    math can be verified against known growth factors."""
    out_dir = tmp_path / "aave_v2_reserve_index" / "chain=1"
    out_dir.mkdir(parents=True)
    pd.DataFrame(rows).to_parquet(out_dir / "fake.parquet")
    return InterestIndexOracle(data_dir=tmp_path)


def _index_row(
    reserve: str,
    block_number: int,
    liquidity: float,
    variable_borrow: float,
    liquidity_rate: float = 0.0,
    variable_borrow_rate: float = 0.0,
) -> dict:
    _RAY = 10**27
    return {
        "chain_id": 1,
        "block_number": block_number,
        "block_timestamp": pd.Timestamp("2022-01-01", tz="UTC")
        + pd.Timedelta(seconds=block_number),
        "reserve": reserve,
        "liquidity_index_raw": str(int(liquidity * _RAY)),
        "variable_borrow_index_raw": str(int(variable_borrow * _RAY)),
        "liquidity_rate_raw": str(int(liquidity_rate * _RAY)),
        "variable_borrow_rate_raw": str(int(variable_borrow_rate * _RAY)),
    }


def test_interest_index_scaling_is_noop_without_oracle(monkeypatch):
    monkeypatch.setattr(engine_module, "_load_interest_index_oracle", lambda: None)
    events = pd.DataFrame(
        [
            _core_event(100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18)),
            _core_event(101, 0, "Borrow", "0xu1", USDC, str(1000 * 10**6)),
        ]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)
    position = engine.account_snapshot("0xu1", block_number=200)

    weth_row = position[position["reserve"] == WETH].iloc[0]
    usdc_row = position[position["reserve"] == USDC].iloc[0]
    assert weth_row["collateral_units"] == pytest.approx(2.0)
    assert usdc_row["debt_units"] == pytest.approx(1000.0)


def test_interest_index_scaling_accrues_collateral_and_debt(tmp_path, monkeypatch):
    oracle = _fake_interest_index_oracle(
        tmp_path,
        [
            _index_row(WETH, 100, liquidity=1.00, variable_borrow=1.00),
            _index_row(WETH, 200, liquidity=1.10, variable_borrow=1.00),
            _index_row(USDC, 100, liquidity=1.00, variable_borrow=1.00),
            _index_row(USDC, 200, liquidity=1.00, variable_borrow=1.05),
        ],
    )
    monkeypatch.setattr(engine_module, "_load_interest_index_oracle", lambda: oracle)

    events = pd.DataFrame(
        [
            _core_event(100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18)),
            _core_event(100, 1, "Borrow", "0xu1", USDC, str(1000 * 10**6)),
        ]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)
    position = engine.account_snapshot("0xu1", block_number=200)

    weth_row = position[position["reserve"] == WETH].iloc[0]
    usdc_row = position[position["reserve"] == USDC].iloc[0]
    # 2 WETH deposited at liquidityIndex=1.00, queried at liquidityIndex=1.10.
    assert weth_row["collateral_units"] == pytest.approx(2.2)
    # 1000 USDC borrowed at variableBorrowIndex=1.00, queried at 1.05.
    assert usdc_row["debt_units"] == pytest.approx(1050.0)


def test_interest_index_scaling_positions_at_many_matches_positions_at(
    tmp_path, monkeypatch
):
    oracle = _fake_interest_index_oracle(
        tmp_path,
        [
            _index_row(WETH, 100, liquidity=1.00, variable_borrow=1.00),
            _index_row(WETH, 200, liquidity=1.10, variable_borrow=1.00),
        ],
    )
    monkeypatch.setattr(engine_module, "_load_interest_index_oracle", lambda: oracle)

    events = pd.DataFrame(
        [_core_event(100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18))]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)

    single = engine.account_snapshot("0xu1", block_number=200)
    batch = engine.positions_at_many(
        pd.DataFrame([{"user": "0xu1", "block_number": 200}])
    )
    batch = batch[batch["reserve"] == WETH]

    assert single[single["reserve"] == WETH].iloc[0][
        "collateral_units"
    ] == pytest.approx(batch.iloc[0]["collateral_units"])
    assert batch.iloc[0]["collateral_units"] == pytest.approx(2.2)


def test_query_timestamp_compounds_index_to_the_trigger_second(tmp_path, monkeypatch):
    # CAS-28 H5 / Lever 8: forward-compound the stored index using its rate,
    # to an exact moment, instead of using the value as of its last write.
    index_block_ts = pd.Timestamp("2022-01-01", tz="UTC") + pd.Timedelta(seconds=100)
    oracle = _fake_interest_index_oracle(
        tmp_path,
        [
            _index_row(
                WETH, 100, liquidity=1.00, variable_borrow=1.00, liquidity_rate=0.05
            ),
            _index_row(
                USDC,
                100,
                liquidity=1.00,
                variable_borrow=1.00,
                variable_borrow_rate=0.05,
            ),
        ],
    )
    monkeypatch.setattr(engine_module, "_load_interest_index_oracle", lambda: oracle)

    events = pd.DataFrame(
        [
            _core_event(100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18)),
            _core_event(100, 1, "Borrow", "0xu1", USDC, str(1000 * 10**6)),
        ]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)

    one_year_later = index_block_ts + pd.Timedelta(days=365)
    no_compounding = engine.positions_at(200)
    compounded = engine.positions_at(200, query_timestamp=one_year_later)

    weth_no_compound = no_compounding[no_compounding["reserve"] == WETH].iloc[0]
    usdc_no_compound = no_compounding[no_compounding["reserve"] == USDC].iloc[0]
    weth_compounded = compounded[compounded["reserve"] == WETH].iloc[0]
    usdc_compounded = compounded[compounded["reserve"] == USDC].iloc[0]
    # Without a query_timestamp: unchanged, index as of its last write (1.00).
    assert weth_no_compound["collateral_units"] == pytest.approx(2.0)
    assert usdc_no_compound["debt_units"] == pytest.approx(1000.0)
    # With a query_timestamp 1 year later at 5% APR: linear (deposits) gives
    # exactly 1.05x; compounded (debt) gives slightly more (~1.0513x).
    assert weth_compounded["collateral_units"] == pytest.approx(2.0 * 1.05)
    assert usdc_compounded["debt_units"] == pytest.approx(
        1000.0 * calculate_compounded_interest(0.05, 365 * 86400)
    )


def test_positions_at_many_query_timestamp_matches_positions_at(tmp_path, monkeypatch):
    oracle = _fake_interest_index_oracle(
        tmp_path,
        [
            _index_row(
                WETH, 100, liquidity=1.00, variable_borrow=1.00, liquidity_rate=0.05
            ),
        ],
    )
    monkeypatch.setattr(engine_module, "_load_interest_index_oracle", lambda: oracle)
    events = pd.DataFrame(
        [_core_event(100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18))]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)

    query_ts = pd.Timestamp("2022-01-01", tz="UTC") + pd.Timedelta(seconds=100 + 86400)
    single = engine.positions_at(200, query_timestamp=query_ts)
    batch = engine.positions_at_many(
        pd.DataFrame(
            [{"user": "0xu1", "block_number": 200, "query_timestamp": query_ts}]
        )
    )
    batch = batch[batch["reserve"] == WETH]

    assert single[single["reserve"] == WETH].iloc[0][
        "collateral_units"
    ] == pytest.approx(batch.iloc[0]["collateral_units"])
    assert batch.iloc[0]["collateral_units"] > 2.0  # accrued, not the raw 2.0 deposit


# ---------------------------------------------------------------------------
# Exact token-level ledger (CAS-28 H3)
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
    block_number: int,
    log_index: int,
    event_type: str,
    reserve: str,
    address_1: str,
    address_2: str | None,
    value: float,
    index: float,
    decimals: int = 18,
    tx_hash: str | None = None,
) -> dict:
    return {
        "block_number": block_number,
        "log_index": log_index,
        "tx_hash": tx_hash or f"0xatok{block_number}_{log_index}",
        "reserve": reserve,
        "event_type": event_type,
        "address_1": address_1,
        "address_2": address_2,
        "value_raw": str(int(round(value * 10**decimals))),
        "index_raw": str(int(round(index * _TEST_RAY))),
    }


def _variable_debt_event(
    block_number: int,
    log_index: int,
    event_type: str,
    reserve: str,
    user: str,
    value: float,
    index: float,
    decimals: int = 18,
    tx_hash: str | None = None,
) -> dict:
    return {
        "block_number": block_number,
        "log_index": log_index,
        "tx_hash": tx_hash or f"0xvdebt{block_number}_{log_index}",
        "reserve": reserve,
        "event_type": event_type,
        "user": user,
        "value_raw": str(int(round(value * 10**decimals))),
        "index_raw": str(int(round(index * _TEST_RAY))),
    }


def _stable_debt_event(
    block_number: int,
    log_index: int,
    event_type: str,
    reserve: str,
    user: str,
    amount: float,
    current_balance: float,
    avg_rate: float,
    timestamp: pd.Timestamp,
    decimals: int = 18,
) -> dict:
    return {
        "block_number": block_number,
        "log_index": log_index,
        "block_timestamp": timestamp,
        "tx_hash": f"0xsdebt{block_number}_{log_index}",
        "reserve": reserve,
        "event_type": event_type,
        "user": user,
        "amount_raw": str(int(round(amount * 10**decimals))),
        "current_balance_raw": str(int(round(current_balance * 10**decimals))),
        "avg_stable_rate_raw": str(int(round(avg_rate * _TEST_RAY))),
    }


def _write_token_ledger(
    tmp_path: Path,
    monkeypatch,
    atoken_rows: list[dict] | None = None,
    vdebt_rows: list[dict] | None = None,
    sdebt_rows: list[dict] | None = None,
) -> None:
    out_dir = tmp_path / "token_ledger"
    out_dir.mkdir(parents=True, exist_ok=True)
    atoken_path = out_dir / "atoken_events.parquet"
    vdebt_path = out_dir / "variable_debt_events.parquet"
    sdebt_path = out_dir / "stable_debt_events.parquet"

    pd.DataFrame(atoken_rows or [], columns=_ATOKEN_EVENT_COLUMNS).to_parquet(
        atoken_path
    )
    pd.DataFrame(vdebt_rows or [], columns=_VDEBT_EVENT_COLUMNS).to_parquet(vdebt_path)
    pd.DataFrame(sdebt_rows or [], columns=_SDEBT_EVENT_COLUMNS).to_parquet(sdebt_path)

    monkeypatch.setattr(engine_module, "_ATOKEN_EVENTS_PATH", atoken_path)
    monkeypatch.setattr(engine_module, "_VARIABLE_DEBT_EVENTS_PATH", vdebt_path)
    monkeypatch.setattr(engine_module, "_STABLE_DEBT_EVENTS_PATH", sdebt_path)


def test_exact_token_ledger_is_noop_without_pull_files(monkeypatch):
    monkeypatch.setattr(engine_module, "_ATOKEN_EVENTS_PATH", Path("/nonexistent"))
    events = pd.DataFrame(
        [_core_event(100, 0, "Deposit", "0xu1", WETH, str(2 * 10**18))]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)
    assert engine.stable_debt_checkpoints is None
    position = engine.account_snapshot("0xu1", block_number=100)
    assert position.iloc[0]["collateral_units"] == pytest.approx(2.0)


def test_exact_token_ledger_mint_and_burn_scales_by_exact_embedded_index(
    tmp_path, monkeypatch
):
    _write_token_ledger(
        tmp_path,
        monkeypatch,
        atoken_rows=[
            _atoken_event(100, 0, "Mint", WETH, "0xu1", None, value=100.0, index=1.0),
            _atoken_event(200, 0, "Burn", WETH, "0xu1", "0xu1", value=30.0, index=1.2),
        ],
    )
    engine = PositionStateEngine(events=pd.DataFrame(), reserve_table=RESERVES)
    position = engine.account_snapshot("0xu1", block_number=300)
    # scaled = 100/1.0 - 30/1.2 = 100 - 25 = 75; no separate InterestIndexOracle
    # in this test, so the scaled value is returned as-is (same "scaled==real
    # when no oracle" convention the pre-H3 path already uses).
    assert position.iloc[0]["collateral_units"] == pytest.approx(75.0)


def test_exact_token_ledger_reinflates_via_interest_index_oracle_at_query_time(
    tmp_path, monkeypatch
):
    oracle = _fake_interest_index_oracle(
        tmp_path, [_index_row(WETH, 300, liquidity=1.5, variable_borrow=1.0)]
    )
    monkeypatch.setattr(engine_module, "_load_interest_index_oracle", lambda: oracle)
    _write_token_ledger(
        tmp_path,
        monkeypatch,
        atoken_rows=[
            _atoken_event(100, 0, "Mint", WETH, "0xu1", None, value=100.0, index=1.0),
        ],
    )
    engine = PositionStateEngine(events=pd.DataFrame(), reserve_table=RESERVES)
    position = engine.account_snapshot("0xu1", block_number=300)
    # scaled (exact embedded index) = 100/1.0 = 100; reinflated at query time
    # by the *separate* InterestIndexOracle's liquidity index (1.5) = 150 --
    # the two index sources (embedded event-time vs. queried query-time)
    # must compose, not conflate.
    assert position.iloc[0]["collateral_units"] == pytest.approx(150.0)


def test_exact_token_ledger_balance_transfer_moves_between_users(tmp_path, monkeypatch):
    _write_token_ledger(
        tmp_path,
        monkeypatch,
        atoken_rows=[
            _atoken_event(100, 0, "Mint", WETH, "0xu1", None, value=100.0, index=1.0),
            _atoken_event(
                150, 0, "BalanceTransfer", WETH, "0xu1", "0xu2", value=40.0, index=1.0
            ),
        ],
    )
    engine = PositionStateEngine(events=pd.DataFrame(), reserve_table=RESERVES)
    sender = engine.account_snapshot("0xu1", block_number=200)
    receiver = engine.account_snapshot("0xu2", block_number=200)
    assert sender.iloc[0]["collateral_units"] == pytest.approx(60.0)
    assert receiver.iloc[0]["collateral_units"] == pytest.approx(40.0)


def test_exact_token_ledger_variable_debt_nets_mint_and_burn(tmp_path, monkeypatch):
    _write_token_ledger(
        tmp_path,
        monkeypatch,
        vdebt_rows=[
            _variable_debt_event(100, 0, "Mint", WETH, "0xu1", value=50.0, index=1.0),
            _variable_debt_event(200, 0, "Burn", WETH, "0xu1", value=20.0, index=1.25),
        ],
    )
    engine = PositionStateEngine(events=pd.DataFrame(), reserve_table=RESERVES)
    position = engine.account_snapshot("0xu1", block_number=300)
    # scaled = 50/1.0 - 20/1.25 = 50 - 16 = 34.
    assert position.iloc[0]["debt_units"] == pytest.approx(34.0)


def test_exact_token_ledger_stable_debt_adds_to_variable_debt(tmp_path, monkeypatch):
    t0 = pd.Timestamp("2022-01-01", tz="UTC")
    _write_token_ledger(
        tmp_path,
        monkeypatch,
        vdebt_rows=[
            _variable_debt_event(100, 0, "Mint", WETH, "0xu1", value=10.0, index=1.0),
        ],
        sdebt_rows=[
            _stable_debt_event(
                100,
                1,
                "Mint",
                WETH,
                "0xu1",
                amount=100.0,
                current_balance=0.0,
                avg_rate=0.20,
                timestamp=t0,
            ),
        ],
    )
    engine = PositionStateEngine(events=pd.DataFrame(), reserve_table=RESERVES)
    position = engine.account_snapshot("0xu1", block_number=200)
    # No query_timestamp given -> stable debt returns its last checkpoint's
    # principal as-is (no forward compounding), matching the "pre-H5-
    # equivalent" convention used elsewhere when no timestamp is supplied.
    # variable (10/1.0=10) + stable (100) = 110, additive, not scaled by the
    # pool's variable index (stable debt has its own locked rate).
    assert position.iloc[0]["debt_units"] == pytest.approx(110.0)


def test_stable_debt_compounds_forward_with_query_timestamp(tmp_path, monkeypatch):
    t0 = pd.Timestamp("2022-01-01", tz="UTC")
    _write_token_ledger(
        tmp_path,
        monkeypatch,
        sdebt_rows=[
            _stable_debt_event(
                100,
                0,
                "Mint",
                WETH,
                "0xu1",
                amount=100.0,
                current_balance=0.0,
                avg_rate=0.05,
                timestamp=t0,
            ),
        ],
    )
    engine = PositionStateEngine(events=pd.DataFrame(), reserve_table=RESERVES)
    query_ts = t0 + pd.Timedelta(days=365)
    position = engine.positions_at(200, query_timestamp=query_ts)
    expected = 100.0 * calculate_compounded_interest(0.05, 365 * 86400)
    assert position[position["reserve"] == WETH].iloc[0]["debt_units"] == pytest.approx(
        expected
    )


def test_exact_token_ledger_liquidation_captured_via_token_events_alone(
    tmp_path, monkeypatch
):
    # No LiquidationCall in `events` at all (in fact `events` is empty) --
    # the seizure/repayment must be fully explained by the token events
    # themselves, since Aave v2's LendingPool.liquidationCall burns debt and
    # seizes collateral through these exact same Mint/Burn/BalanceTransfer
    # calls on-chain (verified against a real liquidation receipt; see
    # backfill_token_ledger_events.py's docstring).
    _write_token_ledger(
        tmp_path,
        monkeypatch,
        atoken_rows=[
            _atoken_event(100, 0, "Mint", WETH, "0xu1", None, value=10.0, index=1.0),
            _atoken_event(
                150, 0, "Burn", WETH, "0xu1", "0xliquidator", value=6.0, index=1.0
            ),
        ],
        vdebt_rows=[
            _variable_debt_event(100, 1, "Mint", WETH, "0xu1", value=8.0, index=1.0),
            _variable_debt_event(150, 1, "Burn", WETH, "0xu1", value=5.0, index=1.0),
        ],
    )
    engine = PositionStateEngine(events=pd.DataFrame(), reserve_table=RESERVES)
    position = engine.account_snapshot("0xu1", block_number=200)
    weth_row = position[position["reserve"] == WETH].iloc[0]
    assert weth_row["collateral_units"] == pytest.approx(4.0)  # 10 - 6 seized
    assert weth_row["debt_units"] == pytest.approx(3.0)  # 8 - 5 repaid


def test_position_at_log_index_excludes_own_transaction_via_tx_hash(
    tmp_path, monkeypatch
):
    # CAS-28 H4-general bugfix: a liquidation's own internal token events
    # (aToken Burn, debt-token Burn) can have a LOWER log_index than its own
    # outer LiquidationCall event -- confirmed on real data (a
    # variableDebtToken Burn at log_index 47 for a LiquidationCall at
    # log_index 56, same tx). Without `tx_hash`, a naive `log_index < cutoff`
    # cut wrongly includes the liquidation's OWN burn as its own "pre-state".
    tx = "0xliqtx"
    _write_token_ledger(
        tmp_path,
        monkeypatch,
        atoken_rows=[
            _atoken_event(100, 0, "Mint", WETH, "0xu1", None, value=10.0, index=1.0),
            # This liquidation's own seizure -- log_index 5, BELOW the outer
            # LiquidationCall's own log_index (10; not itself a token event,
            # so it never appears as a ledger row -- matches the real H3
            # ledger shape).
            _atoken_event(
                200,
                5,
                "Burn",
                WETH,
                "0xu1",
                "0xliquidator",
                value=3.0,
                index=1.0,
                tx_hash=tx,
            ),
        ],
        vdebt_rows=[
            _variable_debt_event(100, 1, "Mint", WETH, "0xu1", value=8.0, index=1.0),
            _variable_debt_event(
                200, 6, "Burn", WETH, "0xu1", value=4.0, index=1.0, tx_hash=tx
            ),
        ],
    )
    engine = PositionStateEngine(events=pd.DataFrame(), reserve_table=RESERVES)

    # Without tx_hash: reproduces the pre-bugfix cutoff -- wrongly includes
    # the liquidation's own burn/repay since 5 < 10 and 6 < 10 alone.
    buggy = engine.position_at_log_index("0xu1", 200, 10)
    buggy_weth = buggy[buggy["reserve"] == WETH].iloc[0]
    assert buggy_weth["collateral_units"] == pytest.approx(7.0)  # 10 - 3 (WRONG)
    assert buggy_weth["debt_units"] == pytest.approx(4.0)  # 8 - 4 (WRONG)

    # With tx_hash: correctly excludes the liquidation's own transaction --
    # true pre-liquidation state, unaffected by its own not-yet-applied burn.
    fixed = engine.position_at_log_index("0xu1", 200, 10, tx_hash=tx)
    fixed_weth = fixed[fixed["reserve"] == WETH].iloc[0]
    assert fixed_weth["collateral_units"] == pytest.approx(10.0)
    assert fixed_weth["debt_units"] == pytest.approx(8.0)


def test_position_at_log_index_after_log_index_preserves_earlier_same_tx_trigger(
    tmp_path, monkeypatch
):
    # 174 real (tx_hash, user) pairs batch >1 LiquidationCall into one tx
    # (e.g. liquidating two debt reserves of the same position back to
    # back). The SECOND liquidation's pre-state must include the FIRST's
    # real effects even though both share tx_hash -- `after_log_index`
    # bounds the same-tx exclusion to strictly after the first's own
    # trigger log_index, so the widening doesn't sweep up (and wrongly
    # exclude) the first's legitimate seizure too.
    tx = "0xbatchtx"
    _write_token_ledger(
        tmp_path,
        monkeypatch,
        atoken_rows=[
            _atoken_event(100, 0, "Mint", WETH, "0xu1", None, value=10.0, index=1.0),
            # First liquidation's own seizure: log_index 5 (< its own outer
            # trigger log_index 10).
            _atoken_event(
                200,
                5,
                "Burn",
                WETH,
                "0xu1",
                "0xliquidator",
                value=2.0,
                index=1.0,
                tx_hash=tx,
            ),
            # Second liquidation's own seizure: log_index 15 (< its own
            # outer trigger log_index 20).
            _atoken_event(
                200,
                15,
                "Burn",
                WETH,
                "0xu1",
                "0xliquidator",
                value=3.0,
                index=1.0,
                tx_hash=tx,
            ),
        ],
    )
    engine = PositionStateEngine(events=pd.DataFrame(), reserve_table=RESERVES)

    # First liquidation (own trigger log_index 10): no earlier trigger in
    # this tx (after_log_index=None) -- sees neither burn.
    before_first = engine.position_at_log_index("0xu1", 200, 10, tx_hash=tx)
    assert before_first[before_first["reserve"] == WETH].iloc[0][
        "collateral_units"
    ] == pytest.approx(10.0)

    # Second liquidation (own trigger log_index 20, after_log_index=10, the
    # first's own trigger log_index): must see the FIRST's real seizure
    # (-2, at log_index 5) but NOT its own (-3, at log_index 15).
    before_second = engine.position_at_log_index(
        "0xu1", 200, 20, tx_hash=tx, after_log_index=10
    )
    assert before_second[before_second["reserve"] == WETH].iloc[0][
        "collateral_units"
    ] == pytest.approx(
        8.0
    )  # 10 - 2, NOT 10 (too much excluded) or 5 (too little)


# ---------------------------------------------------------------------------
# same_block_earlier_tx (CAS-28 H4-general)
# ---------------------------------------------------------------------------


def test_same_block_earlier_tx_detects_different_tx_activity(tmp_path, monkeypatch):
    # A Withdraw (or any other action) earlier in the same block, in a
    # DIFFERENT tx than the trigger -- the case H4-general generalizes to
    # beyond same-user liquidation cascades.
    _write_token_ledger(
        tmp_path,
        monkeypatch,
        atoken_rows=[
            _atoken_event(100, 0, "Mint", WETH, "0xu1", None, value=10.0, index=1.0),
            _atoken_event(
                200,
                5,
                "Burn",
                WETH,
                "0xu1",
                "0xu1",
                value=2.0,
                index=1.0,
                tx_hash="0xwithdraw",
            ),
        ],
    )
    engine = PositionStateEngine(events=pd.DataFrame(), reserve_table=RESERVES)
    keys = pd.DataFrame(
        [
            {
                "user": "0xu1",
                "block_number": 200,
                "log_index": 10,
                "tx_hash": "0xliqtx",
            }
        ]
    )

    result = engine.same_block_earlier_tx(keys)

    assert list(result) == [True]


def test_same_block_earlier_tx_ignores_same_tx_activity(tmp_path, monkeypatch):
    # Deliberately excludes same-tx_hash activity (it can't tell "an earlier
    # trigger's own effects" apart from "this trigger's own effects" from
    # ledger activity alone) -- that's `is_cascade`'s job at the t2_gate
    # level, not this method's.
    tx = "0xbatchtx"
    _write_token_ledger(
        tmp_path,
        monkeypatch,
        atoken_rows=[
            _atoken_event(100, 0, "Mint", WETH, "0xu1", None, value=10.0, index=1.0),
            _atoken_event(
                200,
                5,
                "Burn",
                WETH,
                "0xu1",
                "0xliquidator",
                value=2.0,
                index=1.0,
                tx_hash=tx,
            ),
        ],
    )
    engine = PositionStateEngine(events=pd.DataFrame(), reserve_table=RESERVES)
    keys = pd.DataFrame(
        [{"user": "0xu1", "block_number": 200, "log_index": 10, "tx_hash": tx}]
    )

    result = engine.same_block_earlier_tx(keys)

    assert list(result) == [False]


def test_same_block_earlier_tx_false_without_token_ledger(monkeypatch):
    # Pre-H3 fallback path: self.ledger has no tx_hash column at all, so
    # there's nothing to detect (and nothing to gain from the precise path
    # -- build_ledger's whole-block cuts are already exact there).
    monkeypatch.setattr(engine_module, "_ATOKEN_EVENTS_PATH", Path("/nonexistent"))
    engine = PositionStateEngine(
        events=pd.DataFrame(columns=["event_type"]), reserve_table=RESERVES
    )
    keys = pd.DataFrame(
        [{"user": "0xu1", "block_number": 200, "log_index": 10, "tx_hash": "0xtx"}]
    )

    result = engine.same_block_earlier_tx(keys)

    assert list(result) == [False]


def test_exact_token_ledger_positions_at_many_matches_positions_at_for_stable_only_user(
    tmp_path, monkeypatch
):
    # Regression test for the anchor-row fix: a user with *only* stable debt
    # on a reserve (no aToken/variableDebtToken event ever) must still be
    # discoverable by the batched positions_at_many path, not just the
    # single-key positions_at path.
    t0 = pd.Timestamp("2022-01-01", tz="UTC")
    _write_token_ledger(
        tmp_path,
        monkeypatch,
        sdebt_rows=[
            _stable_debt_event(
                100,
                0,
                "Mint",
                WETH,
                "0xu1",
                amount=100.0,
                current_balance=0.0,
                avg_rate=0.05,
                timestamp=t0,
            ),
        ],
    )
    engine = PositionStateEngine(events=pd.DataFrame(), reserve_table=RESERVES)
    query_ts = t0 + pd.Timedelta(days=365)

    single = engine.positions_at(200, query_timestamp=query_ts)
    batch = engine.positions_at_many(
        pd.DataFrame(
            [{"user": "0xu1", "block_number": 200, "query_timestamp": query_ts}]
        )
    )
    batch = batch[batch["reserve"] == WETH]

    assert not batch.empty
    assert single[single["reserve"] == WETH].iloc[0]["debt_units"] == pytest.approx(
        batch.iloc[0]["debt_units"]
    )


def test_stable_debt_units_handles_checkpoints_out_of_block_order_across_users(
    tmp_path, monkeypatch
):
    # Regression test: `stable_debt_checkpoints` is stored sorted by (user,
    # reserve, block_number) for its other consumer, which does NOT imply a
    # global block_number sort once more than one user is involved --
    # "0xu1"@200 sorts before "0xu2"@100 by (user, ...) but not by
    # block_number alone. `merge_asof` requires the latter; real multi-user
    # data hit this (`ValueError: right keys must be sorted`) even though
    # every single-checkpoint synthetic test above passed (only one row
    # trivially satisfies any sort order).
    t0 = pd.Timestamp("2022-01-01", tz="UTC")
    _write_token_ledger(
        tmp_path,
        monkeypatch,
        sdebt_rows=[
            _stable_debt_event(
                200,
                0,
                "Mint",
                WETH,
                "0xu1",
                amount=50.0,
                current_balance=0.0,
                avg_rate=0.05,
                timestamp=t0,
            ),
            _stable_debt_event(
                100,
                0,
                "Mint",
                WETH,
                "0xu2",
                amount=75.0,
                current_balance=0.0,
                avg_rate=0.05,
                timestamp=t0,
            ),
        ],
    )
    engine = PositionStateEngine(events=pd.DataFrame(), reserve_table=RESERVES)
    batch = engine.positions_at_many(
        pd.DataFrame(
            [
                {"user": "0xu1", "block_number": 300},
                {"user": "0xu2", "block_number": 300},
            ]
        )
    )
    u1 = batch[(batch["user"] == "0xu1") & (batch["reserve"] == WETH)]
    u2 = batch[(batch["user"] == "0xu2") & (batch["reserve"] == WETH)]
    assert u1.iloc[0]["debt_units"] == pytest.approx(50.0)
    assert u2.iloc[0]["debt_units"] == pytest.approx(75.0)


# ---------------------------------------------------------------------------
# Whale-position tracking (CAS-47 subtask)
# ---------------------------------------------------------------------------


class _StubPriceOracle:
    def __init__(self, prices: dict[str, float]):
        self._prices = prices

    def prices_at(
        self, addresses: list[str], timestamp: pd.Timestamp
    ) -> dict[str, float]:
        return {a: self._prices[a] for a in addresses if a in self._prices}


def test_whale_positions_ranks_by_collateral_usd():
    events = pd.DataFrame(
        [
            _core_event(100, 0, "Deposit", "0xwhale", WETH, str(100 * 10**18)),
            _core_event(100, 1, "Deposit", "0xshrimp", WETH, str(1 * 10**18)),
        ]
    )
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)
    oracle = _StubPriceOracle({WETH: 2000.0})

    ranked = engine.whale_positions(
        block_number=100,
        price_oracle=oracle,
        timestamp=pd.Timestamp.now(tz="UTC"),
        top_n=10,
    )
    assert ranked.iloc[0]["user"] == "0xwhale"
    assert ranked.iloc[0]["collateral_usd"] == pytest.approx(200_000.0)


# ---------------------------------------------------------------------------
# Real-data smoke test (skips cleanly without the LFS-pulled data lake)
# ---------------------------------------------------------------------------

pytestmark_real_data = pytest.mark.skipif(
    not _HAS_REAL_DATA, reason="requires ingested data/raw/aave_v2 (Git LFS pull)"
)


@pytestmark_real_data
def test_engine_replays_real_aave_v2_events_without_error():
    events = load_events(data_dir=DATA_DIR, protocol="aave_v2")
    engine = PositionStateEngine(events=events, reserve_table=RESERVES)
    assert not engine.ledger.empty

    # Spot-check: positions_at on the very last observed block should be
    # queryable and non-empty (the engine isn't dropping all history).
    last_block = int(events["block_number"].max())
    snapshot = engine.positions_at(last_block)
    assert not snapshot.empty
