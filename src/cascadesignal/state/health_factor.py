"""Health-factor recompute (CAS-13/CAS-47):

    HF = Σ(collateral_i · price_i · liquidationThreshold_i) / Σ(debt_j · price_j)

T2 (CAS-28) asserts every observed liquidation reconstructs to HF < 1 at its
trigger block; > 2% mismatch blocks the pipeline. That test must be able to
tell a *real* mismatch (the engine/params are wrong) apart from a *coverage*
gap (a reserve in the position has no verified threshold or no price) — so
`compute_health_factor` always reports whether its inputs were complete via
`fully_covered`, instead of silently treating missing data as zero.

It also reports `historical_reliable`: `reserves.reserve_table()`'s risk
parameters are Aave v2's *current*, fully-frozen on-chain state (see that
module's docstring), which for many reserves is a heavily de-risked
liquidation_threshold near zero -- not representative of 2021-2022 values.
A position touching only reserves still flagged `historical_reliable=True`
(WETH/WBTC/stETH/DAI/USDC/AAVE/LINK/TUSD/MKR) is on firmer ground for golden-
episode (China/Terra) HF reconstruction than one touching a de-risked
long-tail reserve; callers should not conflate the two.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

_EPS = 1e-12


@dataclass(frozen=True)
class HealthFactorResult:
    health_factor: float | None  # None iff the account has no debt (HF undefined)
    weighted_collateral_usd: float
    total_debt_usd: float
    fully_covered: (
        bool  # False if any nonzero-balance reserve lacked a price or threshold
    )
    historical_reliable: (
        bool  # False if any nonzero-collateral reserve is de-risked/frozen-era only
    )


def compute_health_factor(
    position: pd.DataFrame,
    prices: dict[str, float],
    reserve_table: pd.DataFrame,
    thresholds_override: dict[str, float] | None = None,
) -> HealthFactorResult:
    """Compute one account's health factor from its per-reserve balances.

    Args:
        position: rows with columns `reserve`, `collateral_units`, `debt_units`
            for a single account (see `engine.PositionStateEngine.positions_at`).
            An optional `collateral_enabled` column (Track B, CAS-28 -- Aave
            v2's per-reserve `setUserUseReserveAsCollateral` toggle) gates
            whether a nonzero collateral balance is weighted into HF at all;
            missing the column defaults every reserve to enabled, matching
            the engine's original unconditional behavior.
        prices: reserve address -> USD price (see `prices.PriceOracle`).
        reserve_table: output of `reserves.reserve_table()`.
        thresholds_override: optional reserve address -> point-in-time
            `liquidation_threshold` (Lever 3, CAS-28 -- the value in effect at
            the block being reconstructed, from `ReserveConfigHistory`). Used
            in place of `reserve_table`'s frozen threshold for any reserve it
            covers; reserves absent from it fall back to the frozen value. A
            reserve priced from the override also counts as
            `historical_reliable` (we have its real historical parameter, so
            the frozen de-risk flag no longer applies to it).
    """
    indexed = reserve_table.set_index("address")
    thresholds = indexed["liquidation_threshold"].to_dict()
    historical_flags = (
        indexed["historical_reliable"].to_dict()
        if "historical_reliable" in indexed.columns
        else {}
    )
    override = thresholds_override or {}

    reserves_arr = position["reserve"].to_numpy()
    collateral_units = position["collateral_units"].to_numpy(dtype=float)
    debt_units = position["debt_units"].to_numpy(dtype=float)
    collateral_enabled = (
        position["collateral_enabled"].to_numpy()
        if "collateral_enabled" in position.columns
        else None
    )

    weighted_collateral = 0.0
    total_debt = 0.0
    fully_covered = True
    historical_reliable = True

    for i in range(len(reserves_arr)):
        reserve = reserves_arr[i]
        price = prices.get(reserve)
        enabled = collateral_enabled is None or bool(collateral_enabled[i])

        if collateral_units[i] > _EPS and enabled:
            overridden = reserve in override
            threshold = override[reserve] if overridden else thresholds.get(reserve)
            if price is None or threshold is None or pd.isna(threshold):
                fully_covered = False
            else:
                weighted_collateral += collateral_units[i] * price * threshold
            # An overridden threshold is the real historical value, so it's
            # reliable regardless of the frozen de-risk flag.
            if not (overridden or historical_flags.get(reserve, False)):
                historical_reliable = False

        if debt_units[i] > _EPS:
            if price is None:
                fully_covered = False
            else:
                total_debt += debt_units[i] * price

    if total_debt <= _EPS:
        return HealthFactorResult(
            None, weighted_collateral, total_debt, fully_covered, historical_reliable
        )

    return HealthFactorResult(
        weighted_collateral / total_debt,
        weighted_collateral,
        total_debt,
        fully_covered,
        historical_reliable,
    )
