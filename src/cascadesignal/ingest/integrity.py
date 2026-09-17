"""T1 ingestion-integrity checks: dedup, USD sanity, reconciliation.

Reconciliation targets come from Aave's published liquidation stats
(https://aave.com/blog/historical-liquidations). Only the China (May 2021)
target is directly comparable against our Aave v2-mainnet-only data, and is
stated explicitly as "Aave v2 alone." The Feb 2026 and lifetime figures are
platform-wide (all Aave versions, all chains) -- Aave v2 mainnet's Feb 2026
liquidation volume is a small fraction of that by construction (v3 is
dominant by then), so comparing it against the platform record would not be
a like-for-like check. Those two are reported for visibility, not asserted
against a tolerance, until Aave v3 liquidations are folded into the same
reconciliation (post-Mock-1 scope).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class ReconciliationTarget:
 name: str
 window_start: str
 window_end: str
 published_events: int
 published_usd: float
 tolerance: float # relative deviation allowed, e.g. 0.2 = 20%
 applicable: bool # False when the published figure isn't v2-only-comparable


# China crackdown: the golden-episode table states this figure
# explicitly as "Aave v2 alone." Empirically the observed cluster spans the
# announcement (mid-May) through the continued deleveraging into June, not
# just May 19 itself -- a single-day window undercounts by >4x.
CHINA_MAY_JUNE_2021 = ReconciliationTarget(
 name="China crackdown (May-Jun 2021)",
 window_start="2021-05-01",
 window_end="2021-07-01",
 published_events=5500,
 published_usd=362_000_000.0,
 tolerance=0.20,
 applicable=True,
)

# Platform-wide (all Aave versions/chains) -- not comparable against v2-only data.
FEB_2026_RECORD = ReconciliationTarget(
 name="Feb 2026 Fed-nomination cascade (platform-wide, reported only)",
 window_start="2026-02-01",
 window_end="2026-03-01",
 published_events=12_500,
 published_usd=429_000_000.0,
 tolerance=float("inf"),
 applicable=False,
)

LIFETIME = ReconciliationTarget(
 name="Lifetime through Feb 2026 (platform-wide, reported only)",
 window_start="2021-01-01",
 window_end="2026-03-01",
 published_events=310_000,
 published_usd=4_650_000_000.0,
 tolerance=float("inf"),
 applicable=False,
)

RECONCILIATION_TARGETS = (CHINA_MAY_JUNE_2021, FEB_2026_RECORD, LIFETIME)


@dataclass(frozen=True)
class ReconciliationResult:
 target: ReconciliationTarget
 observed_events: int
 observed_usd: float
 event_deviation: float # (observed - published) / published
 usd_deviation: float
 within_tolerance: bool


def count_duplicate_events(df: pd.DataFrame) -> int:
 """Count rows sharing a (tx_hash, log_index, user) triple -- should be 0.

 (tx_hash, log_index) alone is too coarse: some batch events (e.g.
 Compound v3 Absorb) legitimately emit multiple rows -- one per affected
 user -- under a single log_index.
 """
 return int(df.duplicated(subset=["tx_hash", "log_index", "user"]).sum)


LIQUIDATION_EVENT_TYPES = frozenset(
 {"LiquidationCall", "LiquidateBorrow", "Absorb", "Bite", "Bark"}
)

# Ceilings are event-type-aware: the largest observed real liquidation across
# all ingested protocols is ~$26M (Aave v3), so anything liquidation-side
# above $500M is almost certainly a decode/decimals bug. Non-liquidation
# events (Deposit/Withdraw/...) legitimately reach the billions for large
# institutional flows -- e.g. a real $1.4B single Aave v2 WETH withdrawal is
# in this dataset -- so they get a much looser ceiling.
DEFAULT_LIQUIDATION_CEILING_USD = 5e8
DEFAULT_GENERAL_CEILING_USD = 5e9


def check_usd_sanity(
 df: pd.DataFrame,
 liquidation_ceiling_usd: float = DEFAULT_LIQUIDATION_CEILING_USD,
 general_ceiling_usd: float = DEFAULT_GENERAL_CEILING_USD,
) -> dict[str, int]:
 """Sanity-check amount_usd: no negatives, no non-finite values, and flag
 outliers above an event-type-aware ceiling (see module docstring)."""
 usd = df["amount_usd"]
 is_liquidation = df["event_type"].isin(LIQUIDATION_EVENT_TYPES)
 ceiling = is_liquidation.map(
 {True: liquidation_ceiling_usd, False: general_ceiling_usd}
 )
 finite = usd.apply(
 lambda x: x is not None and not (isinstance(x, float) and math.isnan(x))
 )
 above_ceiling = (usd > ceiling) & usd.notna
 return {
 "null_usd": int(usd.isna.sum),
 "negative_usd": int((usd.dropna < 0).sum),
 "non_finite_usd": int((~finite).sum - int(usd.isna.sum)),
 "above_ceiling": int(above_ceiling.sum),
 }


def reconcile(df: pd.DataFrame, target: ReconciliationTarget) -> ReconciliationResult:
 """Compare observed liquidation counts/USD in a window against a published target."""
 window = df[
 (df["block_timestamp"] >= target.window_start)
 & (df["block_timestamp"] < target.window_end)
 ]
 observed_events = len(window)
 observed_usd = float(window["amount_usd"].fillna(0.0).sum)

 event_deviation = (
 observed_events - target.published_events
 ) / target.published_events
 usd_deviation = (observed_usd - target.published_usd) / target.published_usd

 within_tolerance = (
 target.applicable
 and abs(event_deviation) <= target.tolerance
 and abs(usd_deviation) <= target.tolerance
 )

 return ReconciliationResult(
 target=target,
 observed_events=observed_events,
 observed_usd=observed_usd,
 event_deviation=event_deviation,
 usd_deviation=usd_deviation,
 within_tolerance=within_tolerance,
 )
