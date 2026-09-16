"""Quantify how much of CAS-28/47's HF-at-trigger mismatch is plausibly
explained by missing interest-index accrual.

Motivation: `cascadesignal.state`'s HF reconstruction has no interest-index
accrual (see `engine.py`'s module docstring). An earlier version of this
script measured real `liquidityIndex` growth from the *start of each golden
episode's pulled window* to each liquidation's trigger block and found it
consistently well under 1% -- but that's a lower bound acknowledged in its
own docstring: a position held since before the pulled window accrued more
than that window-relative measurement could see. Hand-tracing a "typical"
CAS-28 unexplained-mismatch case found exactly that -- the account's real
`Borrow` predated the episode window by months, so most of its true holding
period was invisible to the old measurement. It also only ever measured the
`liquidity` index, even for the debt leg, so it never actually measured
variable-debt growth at all.

CAS-28 Track D (`scripts/onchain/backfill_reserve_index.py`) pulled the full
`ReserveDataUpdated` history over [11.5M, 24.5M] via Etherscan, making
`InterestIndexOracle` coverage continuous rather than 5 disjoint windows.
Combined with `load_events` already returning full core-event history, this
script now measures each liquidated position's *actual* holding period: for
the collateral leg, real `liquidityIndex` growth from the user's last real
`Deposit` on that reserve to the trigger block; for the debt leg, real
`variableBorrowIndex` growth from the user's last real `Borrow` on that
reserve to the trigger block. This directly matters for HF: under-measured
debt growth makes reconstructed HF look *safer* than reality, which is
exactly the T2 gate's failure direction (engine says HF>=1 for a block that
was a real liquidation).

Usage: python scripts/analysis/estimate_interest_accrual_gap.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pandas as pd

from cascadesignal.state.engine import load_events
from cascadesignal.state.interest_index import InterestIndexOracle
from cascadesignal.state.reserves import reserve_table


def _last_event_before_trigger(
    triggers: pd.DataFrame,
    events: pd.DataFrame,
    event_type: str,
    reserve_col: str,
    out_col: str,
) -> pd.DataFrame:
    """For each (user, reserve_col) trigger row, find the same user's most
    recent `event_type` event on that reserve strictly before the trigger
    block, via a per-(user, reserve) backward `merge_asof`."""
    leg = events[events["event_type"] == event_type][
        ["user", "debt_asset", "block_number"]
    ].rename(columns={"debt_asset": reserve_col, "block_number": out_col})
    leg = leg.sort_values(out_col, kind="mergesort")
    left = triggers.sort_values("block_number", kind="mergesort")
    merged = pd.merge_asof(
        left,
        leg,
        left_on="block_number",
        right_on=out_col,
        by=["user", reserve_col],
        direction="backward",
        allow_exact_matches=False,
    )
    return merged


def _measure_leg(
    liquidations: pd.DataFrame,
    events: pd.DataFrame,
    reserve_col: str,
    event_type: str,
    index_kind: str,
    reliable_reserves: set,
    oracle: InterestIndexOracle,
    reserves: pd.DataFrame,
) -> pd.DataFrame:
    triggers = liquidations[
        ["tx_hash", "log_index", "user", "block_number", reserve_col]
    ].dropna(subset=[reserve_col])
    triggers = triggers[triggers[reserve_col].isin(reliable_reserves)]

    matched = _last_event_before_trigger(
        triggers, events, event_type, reserve_col, "last_event_block"
    )
    matched = matched.dropna(subset=["last_event_block"])

    rows = []
    symbol_by_address = reserves.set_index("address")["symbol"]
    for reserve, trigger_block_raw, last_block_raw in zip(
        matched[reserve_col].to_numpy(),
        matched["block_number"].to_numpy(),
        matched["last_event_block"].to_numpy(),
    ):
        trigger_block = int(trigger_block_raw)
        last_block = int(last_block_raw)
        idx_last = oracle.index_at(reserve, last_block, index_kind)
        idx_trigger = oracle.index_at(reserve, trigger_block, index_kind)
        if idx_last is None or idx_trigger is None:
            continue
        days_held = (trigger_block - last_block) * 12 / 86400
        growth_pct = (idx_trigger / idx_last - 1) * 100
        rows.append(
            {
                "leg": reserve_col,
                "reserve": symbol_by_address.get(reserve, reserve),
                "trigger_block": trigger_block,
                "last_event_block": last_block,
                "days_held": round(days_held, 1),
                "index_growth_pct": round(growth_pct, 4),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    oracle = InterestIndexOracle()
    reserves = reserve_table()
    reliable_reserves = set(reserves[reserves["historical_reliable"]]["address"])

    events = load_events(data_dir="data/raw", protocol="aave_v2")
    liquidations = events[events["event_type"] == "LiquidationCall"]

    collateral_leg = _measure_leg(
        liquidations,
        events,
        "collateral_asset",
        "Deposit",
        "liquidity",
        reliable_reserves,
        oracle,
        reserves,
    )
    debt_leg = _measure_leg(
        liquidations,
        events,
        "debt_asset",
        "Borrow",
        "variable_borrow",
        reliable_reserves,
        oracle,
        reserves,
    )
    out = pd.concat([collateral_leg, debt_leg], ignore_index=True)

    if out.empty:
        print("No measurable accrual data found -- check reserve-index coverage.")
        return

    print(f"{len(out)} (leg, reserve, trigger) accrual measurements\n")
    summary = out.groupby(["leg", "reserve"])["index_growth_pct"].agg(
        ["count", "mean", "median", "max"]
    )
    print(summary.to_string())
    print()
    for leg in ("collateral_asset", "debt_asset"):
        leg_out = out[out["leg"] == leg]
        if leg_out.empty:
            continue
        print(
            f"{leg}: n={len(leg_out)}, mean={leg_out['index_growth_pct'].mean():.4f}%, "
            f"median={leg_out['index_growth_pct'].median():.4f}%, "
            f"max={leg_out['index_growth_pct'].max():.4f}%, "
            f"mean days_held={leg_out['days_held'].mean():.1f}"
        )


if __name__ == "__main__":
    main()
