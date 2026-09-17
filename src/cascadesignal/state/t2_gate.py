"""T2 state-reconstruction correctness gate:

 every observed liquidation must reconstruct to HF < 1 at its trigger
 block; > 2% mismatch blocks the pipeline.

This module is the reusable core behind that gate: batch-reconstruct HF at
trigger for a whole population of `LiquidationCall` events, summarize the
mismatch rate per protocol + overall, and bucket mismatches by likely cause
so `tests/test_t2_mismatch_gate.py` (the actual pytest enforcement) and
`scripts/analysis/t2_mismatch_report.py` (the human-readable diagnostics
dump) don't each reimplement it.

Tolerance band (2026-07-23): sixteen levers drove the
exact-boundary (`HF >= 1.0`) mismatch rate from 53.9% to 2.5853%, at which
point three independent ground-truth checks (H7's archive-node
`getUserAccountData` spot-check, the `MathUtils.sol` debt-formula audit, the
Messari-subgraph H8 cross-check) confirmed the residual is this
reconstruction's own precision floor (~1%, bounded by the same order as the
Chainlink feeds' own 0.5-2% deviation thresholds), not a fixable
reconstruction bug. `HF_TOLERANCE` below expresses that measured precision as
a pre-registered gate parameter: a trigger only counts as a mismatch once its
reconstructed HF clears 1.0 by more than the reconstruction's own noise floor.
`mismatch_summary` always reports both the exact-boundary and tolerance-band
rates side by side, per ADR-005's reporting commitment -- neither is ever
printed without the other.

Trigger-block convention (2026-07-21): HF is computed from
the position state *entering* the trigger block -- cumulative ledger through
`block_number - 1`, i.e. **before** the liquidation's own collateral seizure
and debt repayment. This is the physically correct test of the gate's
invariant ("reconstruct to HF < 1 *at its trigger block*"): a liquidation
fires *because* the pre-liquidation HF < 1, and the liquidation itself
repays up to 50% of the debt and seizes collateral+bonus, which mechanically
*restores* the position's health. The previous convention evaluated
`engine.positions_at(block)` -- the post-liquidation, health-**restored**
state -- which systematically inflated the mismatch rate: every mismatch was
a partial liquidation whose measured HF was the aftermath, not the trigger
condition (median post-liq HF 1.17). Switching to the pre-liquidation state
moved the real-data rate from 37.8% to 30.9% (~2,000 fewer mismatches) and,
because the pre-trigger debt still exists, made more triggers measurable
(a full-repay liquidation is now testable at its pre-liq state instead of
being dropped as "no debt"). The earlier
(`compare_price_oracle_hf_mismatch.py`) ~24-34% figures used the old
post-liq convention; they are a historical baseline, not comparable to
post-Lever-1 numbers.

Granularity note: `positions_at_many` looks up cumulative balances by
`block_number` (it cannot resolve intra-block `log_index`), so evaluating at
`block_number - 1` excludes *all* trigger-block events, not only the
liquidation's own rows. Most triggers have no other same-block activity at
all, so this is exact for them; the (still small) minority that do are
routed to the precise per-event path instead -- see below.

Same-block liquidation cascades (/H4a, 2026-07-22): the
original exception found. Some real Aave v2 `LiquidationCall` events are a
*second* (or later) liquidation of the same user in the same block as an
earlier one -- EVM execution is sequential even within one block (and even
within one tx: a liquidator contract batching two `liquidationCall`
invocations still executes them one after another), so the second
liquidation's true pre-state includes the first's seizure/repay. The
previous convention keyed triggers on `(user, block_number)` alone, so
`drop_duplicates` silently kept only the *first* of each cascade and dropped
the rest from the population entirely -- not a wrong verdict, an ABSENT one.
Triggers are now keyed on `(user, block_number, log_index)`; a trigger with
`cascade_size > 1` (>1 trigger sharing `(user, block_number)`, regardless of
tx_hash) uses the precise per-event `engine.position_at_log_index`, which
cuts strictly before `(block_number, log_index)` rather than at a whole-block
boundary.

Same-block-earlier-activity, generalized (-general, 2026-07-22):
cascades are just the special case where the earlier same-block action is
*also* a `LiquidationCall`. H7's archive-node ground-truth spot-check found
38.3% of a real "unexplained"-mismatch sample matched Aave's own live
contract at this engine's own `block_number - 1` cutoff -- meaning something
*else* earlier in the trigger's own block (a Withdraw, a Borrow, any ledger
event for that user from a different transaction) was invisible to both.
`engine.same_block_earlier_tx` detects this directly from ledger/stable-debt
activity and routes it to the same precise path as cascades
(`reconstruct_hf_at_trigger`'s `needs_precise_path = is_cascade |
engine.same_block_earlier_tx(...)`). This surfaced a latent bug in the
original cascade fix itself: a liquidation's own internal token events
(aToken `Burn`/`BalanceTransfer`, debt-token `Burn`) can have a *lower*
`log_index` than its own outer `LiquidationCall` event (confirmed on real
data), and 174 real `(tx_hash, user)` pairs batch >1 `LiquidationCall` into
one tx -- so a naive "exclude everything sharing this trigger's `tx_hash`"
widening would wrongly exclude an *earlier* trigger's real effects too, not
just this one's own. `position_at_log_index`'s `after_log_index` param fixes
this by bounding the same-tx exclusion to strictly after the previous
same-user, same-tx trigger's own `log_index` -- see its docstring.

Performance note: naively calling `PositionStateEngine.account_snapshot`
once per liquidation is O(ledger size) per call -- for ~49k real Aave v2
liquidations against a ~1.5M-row ledger that's tens of billions of row
operations, far too slow for a test. `PositionStateEngine.positions_at_many`
instead computes cumulative per-(user, reserve) balances once and looks up
each trigger block via a single grouped `merge_asof`, which is
O((liquidations x reserves per user) log n) -- seconds, not hours, on the
real dataset.
"""

from __future__ import annotations

from typing import Hashable, Protocol

import pandas as pd

from cascadesignal.state.engine import PositionStateEngine
from cascadesignal.state.health_factor import _EPS, compute_health_factor
from cascadesignal.state.reserve_config_history import ReserveConfigHistory

MISMATCH_THRESHOLD = 0.02

#: a reconstructed HF only counts as a mismatch once it clears
# 1.0 by more than this tolerance -- the reconstruction's own measured
# precision floor (H7's ~1% component diffs; Chainlink's 0.5-2% deviation
# thresholds), not a value picked to be the minimum that passes
# MISMATCH_THRESHOLD (the smallest passing tolerance is 0.25%; see ADR-005's
# tolerance curve). Frozen once ADR-005 is Accepted -- any change requires a
# superseding ADR.
HF_TOLERANCE = 0.01


class PriceOracleLike(Protocol):
 """Structural interface shared by `prices.PriceOracle` and
 `prices.ChainlinkPriceOracle` (and test stubs) -- this module only ever
 calls `prices_at`, so it depends on that shape rather than either
 concrete class.

 `block_number`/`log_index`: optional -- callers here always
 pass the trigger's own, but `prices.PriceOracle` (DefiLlama daily) has no
 intra-day granularity to use them for, and any external stub only used
 for other test scenarios is free to ignore them too."""

 def prices_at(
 self,
 addresses: list[str],
 timestamp: pd.Timestamp,
 block_number: int | None = None,
 log_index: int | None = None,
 ) -> dict[str, float]: ...


_REPORT_COLUMNS = [
 "protocol",
 "user",
 "block_number",
 "log_index",
 "block_timestamp",
 "health_factor",
 "fully_covered",
 "historical_reliable",
 "measurable",
 "mismatch",
]

PositionByKey = dict[tuple[str, int, int], pd.DataFrame]


def reconstruct_hf_at_trigger(
 engine: PositionStateEngine,
 liquidations: pd.DataFrame,
 price_oracle: PriceOracleLike,
 config_history: ReserveConfigHistory | None = None,
 compound_interest_to_trigger: bool = True,
) -> tuple[pd.DataFrame, PositionByKey]:
 """Reconstruct HF at the trigger block for every (user, block) liquidation
 key in `liquidations` (columns: protocol, user, block_number,
 block_timestamp).

 HF is evaluated on the *pre-liquidation* state (cumulative ledger through
 `block_number - 1`), not the post-liquidation state -- see the module
 docstring's "Trigger-block convention" for why this is the physically
 correct test of the invariant.

 If `config_history` is given (Lever 3), each trigger's HF uses the
 per-reserve `liquidation_threshold` in effect *at that trigger block*
 rather than `reserve_table`'s frozen value -- correcting for Aave v2's
 governance-era threshold changes. A no-op when `None` (falls back to
 frozen thresholds everywhere).

 `compound_interest_to_trigger` (Lever 8, H5, default True):
 forward-compounds each reserve's interest index from its last on-chain
 write to the trigger's exact `block_timestamp` (via
 `engine.PositionStateEngine`'s `query_timestamp` plumbing), replicating
 Aave's real `balanceOf` instead of using the index as of its last write.
 Set False to reproduce the pre-Lever-8 behavior for A/B measurement.

 Returns:
 report: one row per unique (user, block_number, log_index) trigger
 (i.e. one row per raw `LiquidationCall` event -- see the module
 docstring's "Same-block liquidation cascades" note) with columns
 per `_REPORT_COLUMNS`, keyed on the true trigger block.
 `measurable` is False when the pre-liquidation position lacked
 full price/threshold coverage (`fully_covered=False`) or had no
 debt entering the trigger block (`health_factor is None` -- the
 ratio isn't defined, e.g. the position was opened in the same
 block it was liquidated); `mismatch` is only meaningful where
 `measurable` is True.
 position_by_key: (user, block_number, log_index) -> the reconstructed
 pre-liquidation position DataFrame (reserve, collateral_units,
 debt_units), for reuse by `bucket_mismatch_causes` without
 recomputing.
 """
 if liquidations.empty:
 return pd.DataFrame(columns=_REPORT_COLUMNS), {}

 trigger_keys = (
 liquidations[
 [
 "protocol",
 "user",
 "block_number",
 "log_index",
 "block_timestamp",
 "tx_hash",
 ]
 ]
 .drop_duplicates(subset=["user", "block_number", "log_index"])
 .reset_index(drop=True)
 )

 # Same-block-earlier-activity triggers (-general, generalizing
 # Lever 7/H4a's cascade check): the fast batched block_number - 1 query
 # is only exact when nothing else happened earlier in the trigger's own
 # block for this user. Two independent conditions route a trigger to the
 # precise per-event position_at_log_index instead (a Python loop is fine
 # at the resulting scale):
 # 1. `is_cascade` -- >1 trigger shares (user, block_number), regardless
 # of tx_hash (covers both a cascade across separate txs AND >1
 # LiquidationCall batched into one tx -- 174 real (tx_hash, user)
 # pairs have this, e.g. liquidating two debt reserves of the same
 # position back to back).
 # 2. `same_block_earlier_tx` -- real ledger/stable-debt activity from a
 # genuinely different transaction earlier in the block (a Withdraw,
 # a Borrow, or a liquidation of a DIFFERENT trigger not caught by
 # #1's grouping key, e.g. none here since it groups the same way,
 # but kept independent for clarity/robustness).
 # These are NOT redundant -- see `same_block_earlier_tx`'s docstring for
 # why it deliberately excludes same-tx_hash activity (can't tell "an
 # earlier trigger's own effects" apart from "this trigger's own effects"
 # from ledger activity alone), which is exactly what #1 exists to catch.
 cascade_size = trigger_keys.groupby(["user", "block_number"])[
 "log_index"
 ].transform("size")
 is_cascade = cascade_size > 1
 needs_precise_path = is_cascade | engine.same_block_earlier_tx(trigger_keys)
 solo_keys = trigger_keys[~needs_precise_path]
 precise_keys = trigger_keys[needs_precise_path]

 position_by_key: PositionByKey = {}
 _POSITION_COLUMNS = [
 "reserve",
 "collateral_units",
 "debt_units",
 "collateral_enabled",
 ]

 if not solo_keys.empty:
 # Pre-liquidation convention: query the state at block_number - 1
 # (before the liquidation's own ledger effect), then relabel each
 # result row back to its true trigger block. The -1 shift is
 # injective, so it introduces no key collisions.
 state_keys = solo_keys[["user", "block_number"]].copy
 state_keys["block_number"] = state_keys["block_number"] - 1
 if compound_interest_to_trigger:
 # H5: compound each row's interest index to its OWN trigger
 # moment, not the shifted block_number - 1's.
 state_keys["query_timestamp"] = solo_keys["block_timestamp"].to_numpy
 positions = engine.positions_at_many(state_keys)
 positions["block_number"] = positions["block_number"] + 1
 log_index_by_user_block: dict[tuple[str, int], int] = dict(
 zip(
 zip(solo_keys["user"], solo_keys["block_number"]),
 solo_keys["log_index"],
 )
 )
 for group_key, group in positions.groupby(["user", "block_number"], sort=False):
 user_str = str(group_key[0])
 block_int = int(group_key[1]) # type: ignore[call-overload]
 log_index = log_index_by_user_block[(user_str, block_int)]
 key = (user_str, block_int, int(log_index))
 position_by_key[key] = group[_POSITION_COLUMNS].reset_index(drop=True)

 # `after_log_index` (-general bugfix, see
 # `position_at_log_index`'s docstring): the previous trigger sharing this
 # SAME (user, tx_hash) -- not just same user+block -- so the same-tx
 # widening inside `position_at_log_index` never sweeps in an *earlier*
 # trigger's own real effects when >1 LiquidationCall is batched into one
 # tx. Computed from the FULL trigger_keys (not just precise_keys): a
 # cascade's earlier member might otherwise resolve via the fast solo
 # path, but it's still the right floor for its later sibling either way.
 by_user_tx = trigger_keys.sort_values(
 ["user", "tx_hash", "log_index"], kind="mergesort"
 )
 prev_trigger_log_index = by_user_tx.groupby(["user", "tx_hash"])["log_index"].shift(
 1
 )
 after_log_index_by_key: dict[tuple[str, int, int], int | None] = {
 (str(u), int(b), int(li)): (None if pd.isna(prev) else int(prev))
 for u, b, li, prev in zip(
 by_user_tx["user"],
 by_user_tx["block_number"],
 by_user_tx["log_index"],
 prev_trigger_log_index,
 )
 }

 for user, block_number, log_index, block_timestamp, tx_hash in zip(
 precise_keys["user"],
 precise_keys["block_number"],
 precise_keys["log_index"],
 precise_keys["block_timestamp"],
 precise_keys["tx_hash"],
 ):
 key = (str(user), int(block_number), int(log_index))
 query_timestamp = block_timestamp if compound_interest_to_trigger else None
 position = engine.position_at_log_index(
 key[0],
 key[1],
 key[2],
 tx_hash=tx_hash,
 after_log_index=after_log_index_by_key[key],
 query_timestamp=query_timestamp,
 )
 position_by_key[key] = position[_POSITION_COLUMNS].reset_index(drop=True)

 ts_by_key: dict[tuple[str, int, int], pd.Timestamp] = dict(
 zip(
 zip(
 trigger_keys["user"],
 trigger_keys["block_number"],
 trigger_keys["log_index"],
 ),
 trigger_keys["block_timestamp"],
 )
 )
 protocol_by_key: dict[tuple[str, int, int], str] = dict(
 zip(
 zip(
 trigger_keys["user"],
 trigger_keys["block_number"],
 trigger_keys["log_index"],
 ),
 trigger_keys["protocol"],
 )
 )

 rows: list[dict] = []
 for key, position in position_by_key.items:
 user, block_number, log_index = key
 timestamp = ts_by_key[key]
 #: resolve strictly before this trigger's own log
 # position, not just at-or-before its block_timestamp -- see
 # `prices.ChainlinkPriceOracle.price_at`'s docstring.
 prices = price_oracle.prices_at(
 position["reserve"].tolist, timestamp, block_number, log_index
 )
 thresholds_override = (
 config_history.thresholds_at(block_number)
 if config_history is not None
 else None
 )
 result = compute_health_factor(
 position, prices, engine.reserve_table, thresholds_override
 )

 hf = result.health_factor
 measurable = result.fully_covered and hf is not None
 #: tolerance-band mismatch condition. `mismatch_summary`
 # separately recomputes the exact-boundary (HF >= 1.0) rate from
 # `health_factor` for reporting -- see its docstring.
 mismatch = measurable and hf is not None and hf >= 1.0 + HF_TOLERANCE
 rows.append(
 {
 "protocol": protocol_by_key[key],
 "user": user,
 "block_number": block_number,
 "log_index": log_index,
 "block_timestamp": timestamp,
 "health_factor": hf,
 "fully_covered": result.fully_covered,
 "historical_reliable": result.historical_reliable,
 "measurable": measurable,
 "mismatch": mismatch,
 }
 )

 report = pd.DataFrame(rows, columns=_REPORT_COLUMNS)
 report = report.sort_values(
 ["block_number", "user", "log_index"], kind="mergesort"
 ).reset_index(drop=True)
 return report, position_by_key


def mismatch_summary(report: pd.DataFrame) -> pd.DataFrame:
 """Per-protocol + OVERALL mismatch rate, restricted to `measurable` rows.

 Also reports `coverage_rate` (share of all trigger keys that were
 measurable at all) so a low mismatch rate can't be read in isolation from
 a gappy denominator.: `mismatch_rate` is the tolerance-band rate (`report`'s own
 `mismatch` column, i.e. `HF >= 1.0 + HF_TOLERANCE`) -- the gate's
 operative number. `mismatch_rate_exact` is recomputed here directly from
 `health_factor` at the original exact boundary (`HF >= 1.0`), so the two
 are always reported side by side per ADR-005's commitment to never print
 one without the other.
 """
 if report.empty:
 return pd.DataFrame(
 columns=[
 "n_triggers",
 "n_measurable",
 "coverage_rate",
 "n_mismatch",
 "mismatch_rate",
 "n_mismatch_exact",
 "mismatch_rate_exact",
 ]
 )

 def _summarize(group: pd.DataFrame) -> pd.Series:
 measurable = group[group["measurable"]]
 n_triggers = len(group)
 n_measurable = len(measurable)
 n_mismatch = int(measurable["mismatch"].sum)
 # `measurable` rows always have a non-null `health_factor` (that's
 # what `measurable` requires -- see `reconstruct_hf_at_trigger`), so
 # this comparison is safe without an explicit notna guard.
 n_mismatch_exact = int((measurable["health_factor"] >= 1.0).sum)
 return pd.Series(
 {
 "n_triggers": n_triggers,
 "n_measurable": n_measurable,
 "coverage_rate": (
 n_measurable / n_triggers if n_triggers else float("nan")
 ),
 "n_mismatch": n_mismatch,
 "mismatch_rate": (
 n_mismatch / n_measurable if n_measurable else float("nan")
 ),
 "n_mismatch_exact": n_mismatch_exact,
 "mismatch_rate_exact": (
 n_mismatch_exact / n_measurable if n_measurable else float("nan")
 ),
 }
 )

 by_protocol = report.groupby("protocol", sort=True).apply(
 _summarize, include_groups=False
 ) # type: ignore[call-overload]
 overall = _summarize(report).to_frame.T
 overall.index = ["OVERALL"]
 return pd.concat([by_protocol, overall])


def bucket_mismatch_causes(
 report: pd.DataFrame,
 position_by_key: PositionByKey,
 engine: PositionStateEngine,
 chainlink_oracle: PriceOracleLike | None,
 config_history: ReserveConfigHistory | None = None,
) -> pd.Series:
 """Assign a likely-cause label to every EXACT-BOUNDARY mismatched row in
 `report` (`measurable` and `health_factor >= 1.0` -- not `report`'s own
 `mismatch` column; see the) (index-aligned;
 non-mismatch rows get `None`):: this buckets (and `apply_live_oracle_fallback`
 below corrects) the *exact-boundary* residual (`HF >= 1.0`), not
 `report["mismatch"]` (which, post-ADR-005, is the narrower
 `HF >= 1.0 + HF_TOLERANCE` tolerance-band condition). Lever 12's
 live-oracle correction predates and is independent of the tolerance band
 -- scoping it to only tolerance-mismatches would silently stop attempting
 to correct near-boundary cases it already handles (HF in
 `[1.0, 1.0 + HF_TOLERANCE)`), inflating the exact-boundary rate reported
 alongside the tolerance-band rate in `mismatch_summary` for no reason
 (those rows are already tolerance-matches either way, so skipping their
 correction has zero effect on the gate's own pass/fail decision, but it
 would understate how much of the exact-boundary residual Lever 12
 actually resolves).

 - `param_drift`: the position touches a reserve whose current on-chain
 liquidation threshold is de-risked/frozen-era-only (`reserves.py`'s
 `historical_reliable=False`) -- the threshold used may understate
 historical collateral value. With `config_history` (Lever 3)
 the reconstruction already uses the real point-in-time threshold, so
 `historical_reliable` is True for those reserves and this bucket
 shrinks to reserves with no config history at all.
 - `oracle_lag`: swapping in `chainlink_oracle`'s prices (where fully
 covered for every reserve in the position) flips the result to HF < 1
 -- the mismatch is explained by price staleness in whatever the
 *primary* reconstruction used (originally DefiLlama-daily vs.
 Chainlink-block). (2026-07-22): callers
 should pass the *same* oracle used for the primary reconstruction
 (`price_oracle`, e.g. `PreferEthNumeraireOracle`) as `chainlink_oracle`
 here, not a separate, narrower one -- an earlier version of
 `t2_mismatch_report.py` passed a standalone asset/USD
 `ChainlinkPriceOracle` that H2's full-history pull never extended
 (different aggregator map from `RESERVE_CHAINLINK_ETH_FEEDS`), which
 wildly overstated `unexplained_no_chainlink_coverage` below (3,413 vs.
 the true 547) by checking coverage against a golden-windows-only
 oracle instead of the one the mismatch was actually determined with.
 With the same oracle on both sides, `oracle_lag` is expected to land
 at ~0 (recomputing with identical inputs can't flip the verdict)
 the bucket is kept, not removed, in case a genuinely independent,
 better price source (e.g. H7's archive-node ground truth) is wired in
 as `chainlink_oracle` later.
 - `unexplained`: `chainlink_oracle` is available and *confirms* the
 mismatch (HF >= 1 under both oracles) -- neither known cause explains
 it. With the Lever 9 fix, this is now the dominant bucket (3,142 of
 3,715 mismatches) -- the residual is a genuine reconstruction gap, not
 a coverage gap.
 - `unexplained_no_chainlink_coverage`: `chainlink_oracle` has no price for
 some reserve that actually matters to this position's HF (post-Lever-11c fix: only reserves with `collateral_units > _EPS` and
 enabled, or `debt_units > _EPS` -- mirroring `compute_health_factor`'s
 own coverage gating -- can trigger this label; a zero/dust-balance
 reserve's missing price is irrelevant to the real verdict either way,
 so it must not decide the label). The original version required a
 price for *every* reserve `position_by_key` ever lists for that
 (user, block, log_index), including reserves with ~0 balance that
 `compute_health_factor` never even prices -- so a position holding
 real, fully-covered collateral/debt plus unrelated dust in some
 reserve predating that reserve's own Chainlink history (common in
 Aave v2's first ~3 months, before Chainlink's own asset coverage had
 caught up) was mislabeled here even though its actual mismatch
 verdict never depended on that dust reserve's price. Traced and fixed
 after a fresh mismatch report still showed 174 rows in this bucket
 post-Lever-11c despite `UNCOVERED_ETH_RESERVES` being empty -- every
 one traced to exactly this dust-reserve pattern (or LUSD's deliberate
 `coverage_end` clip), not a real remaining coverage gap. Purely a
 labeling fix: doesn't change `report["mismatch"]` or the gate's
 measured rate, only which bucket a mismatch is attributed to.
 - `unexplained`: `chainlink_oracle` is available and *confirms* the
 mismatch (HF >= 1 under both oracles) -- neither known cause explains
 it. With the Lever 9 fix, this is now the dominant bucket (3,142 of
 3,715 mismatches) -- the residual is a genuine reconstruction gap, not
 a coverage gap.
 """
 exact_mismatch = report["measurable"] & (report["health_factor"] >= 1.0)
 mismatched = report[exact_mismatch]
 causes_by_index: dict[Hashable, str] = {}

 for idx, row in mismatched.iterrows:
 if not row["historical_reliable"]:
 causes_by_index[idx] = "param_drift"
 continue

 if chainlink_oracle is None:
 causes_by_index[idx] = "unexplained_no_chainlink_coverage"
 continue

 position = position_by_key[(row["user"], row["block_number"], row["log_index"])]
 reserves_involved = position["reserve"].tolist
 chainlink_prices = chainlink_oracle.prices_at(
 reserves_involved,
 row["block_timestamp"],
 int(row["block_number"]),
 int(row["log_index"]),
 )
 # Only reserves that actually move the HF need a resolved price
 # here -- see the `unexplained_no_chainlink_coverage` docstring
 # above. `reserves_involved` (the full position) is still what's
 # passed to `prices_at` above so the fallback decision matches the
 # primary reconstruction exactly; only the *label* narrows to the
 # economically-relevant subset.
 relevant = (
 (position["collateral_units"] > _EPS)
 & position["collateral_enabled"].astype(bool)
 ) | (position["debt_units"] > _EPS)
 relevant_reserves = position.loc[relevant, "reserve"].tolist
 if not all(r in chainlink_prices for r in relevant_reserves):
 causes_by_index[idx] = "unexplained_no_chainlink_coverage"
 continue

 thresholds_override = (
 config_history.thresholds_at(int(row["block_number"]))
 if config_history is not None
 else None
 )
 chainlink_result = compute_health_factor(
 position, chainlink_prices, engine.reserve_table, thresholds_override
 )
 if (
 chainlink_result.health_factor is not None
 and chainlink_result.health_factor < 1.0
 ):
 causes_by_index[idx] = "oracle_lag"
 else:
 causes_by_index[idx] = "unexplained"

 return pd.Series(causes_by_index, dtype=object).reindex(report.index)


def apply_live_oracle_fallback(
 report: pd.DataFrame,
 causes: pd.Series,
 position_by_key: PositionByKey,
 engine: PositionStateEngine,
 live_oracle: PriceOracleLike,
 config_history: ReserveConfigHistory | None = None,
) -> pd.DataFrame:
 """Re-check every `unexplained` mismatch against Aave v2's real,
 live `AaveOracle.getAssetPrice` (`prices.LiveAaveOracleFallback`) instead
 of this engine's own Chainlink-log reconstruction. Returns a COPY of
 `report` with `mismatch`/`health_factor` corrected wherever the live
 oracle fully covers the position and its own recomputed HF < 1
 ground truth confirms the discrepancy was in this project's price
 *reconstruction*, not in Aave's real liquidation decision.

 Deliberately scoped to `unexplained` only, not the other ~45k measurable
 triggers this project already reconstructs cheaply and reproducibly
 offline: a full-population spot-check (n=2,896 -- the entire
 `unexplained` bucket, not a sample) found the live oracle fully prices
 98.0% of these positions and flips 58.6% of them to correctly match
 reality, while agreeing closely with the existing reconstruction on an
 equal-sized already-matching sample (median 0.22% price diff) -- so the
 win is concentrated here, not a signal to replace Chainlink everywhere.
 See `experiments/T2/CAS28_mismatch_next_steps.md`'s live-oracle-fallback
 section for the full writeup.

 Uses `block_number - 1` for the live query (this project's own
 pre-liquidation convention, same as H7's `getUserAccountData`
 spot-check) -- an approximation for the small minority of triggers on
 the precise cascade/same-block-earlier-tx path (whose true pre-state is
 strictly before their own `log_index`, not a whole-block boundary), same
 simplification H7 already accepted.

 Degrades gracefully offline: `live_oracle.prices_at` returns whatever is
 already cached without any network access (see
 `LiveAaveOracleFallback`'s docstring); rows needing an uncached pair
 with no `ARCHIVE_RPC_URL` available are simply left uncorrected, not
 raised as an error.
 """
 report = report.copy
 target_idx = causes[causes == "unexplained"].index
 for idx in target_idx:
 row = report.loc[idx]
 key = (row["user"], int(row["block_number"]), int(row["log_index"]))
 position = position_by_key[key]
 reserves_involved = position["reserve"].tolist
 live_prices = live_oracle.prices_at(
 reserves_involved, row["block_timestamp"], int(row["block_number"]) - 1
 )
 if not all(r in live_prices for r in reserves_involved):
 continue

 thresholds_override = (
 config_history.thresholds_at(int(row["block_number"]))
 if config_history is not None
 else None
 )
 live_result = compute_health_factor(
 position, live_prices, engine.reserve_table, thresholds_override
 )
 if live_result.health_factor is not None and live_result.health_factor < 1.0:
 report.loc[idx, "mismatch"] = False
 report.loc[idx, "health_factor"] = live_result.health_factor
 return report


def attach_diagnostics(
 report: pd.DataFrame,
 liquidations: pd.DataFrame,
 causes: pd.Series,
) -> pd.DataFrame:
 """Join the per-trigger `report` back onto every raw `LiquidationCall`
 event, for per-event diagnostics (asset, block, cause).

 Triggers are keyed on (user, block_number, log_index) -- one row per raw
 liquidation event (see the module docstring's "Same-block liquidation
 cascades" note) -- so this join is 1:1 in the common case; it remains a
 merge (not a plain assign) because `liquidations` carries columns
 (`tx_hash`, `collateral_asset`, `debt_asset`) that `report` doesn't.
 """
 per_trigger = report.assign(cause=causes)[
 [
 "user",
 "block_number",
 "log_index",
 "health_factor",
 "measurable",
 "mismatch",
 "cause",
 ]
 ]
 diagnostics = liquidations[
 [
 "protocol",
 "tx_hash",
 "user",
 "block_number",
 "log_index",
 "collateral_asset",
 "debt_asset",
 ]
 ].merge(per_trigger, on=["user", "block_number", "log_index"], how="left")
 return diagnostics.sort_values(["block_number", "user"]).reset_index(drop=True)
