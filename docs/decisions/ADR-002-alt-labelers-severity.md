# ADR-002: Alternative cascade definitions D-B/D-C and the full severity measure

- **Status:** Accepted
- **Date:** 2026-07-14
- **Ticket:** CAS-29 (P2, Labeling)
- **Depends on:** ADR-001 (D-A primary definition), CAS-11 (D-A labeler, Done)
- **Blocks:** CAS-4 (M6 EVT severity), E7 (definition-sensitivity analysis)
- **Note on ticket numbering:** ADR-001 refers to this deferred work as
  "CAS-17" and its blockers as "CAS-8" (DEX depth) / "CAS-13" (state
  reconstruction) — the Implementation Board's ticket numbers were reassigned
  after ADR-001 was written. Current numbers: this ticket is **CAS-29**, DEX
  depth ingestion is **CAS-16**, the state-reconstruction engine is **CAS-47**.
  This ADR uses current numbering throughout.

## Context

ADR-001 pre-registered D-A (rolling window + generation-linking) as the
primary cascade definition and reserved D-B (Hawkes declustering) and D-C
(peaks-over-threshold) as alternative definitions for the definition-
sensitivity analysis (E7) — RQ2's conclusion must not depend on an
arbitrarily chosen cascade definition. ADR-001 named the algorithms but did
not fix their parameters. Per this project's pre-registration discipline
(PLAN §9), that has to happen in an ADR before the labeler code runs, exactly
as D-A's grid was fixed before CAS-11 executed it — otherwise a reviewer
cannot rule out the parameters having been tuned to produce a convenient E7
result.

This ADR also fixes the full severity measure (USD liquidated + realized
price impact + bad debt), which ADR-001 explicitly deferred.

## Decision

### D-B: Hawkes declustering

A single global marked-free exponential-kernel Hawkes process is fit per
`protocol_tag` on the full liquidation point process (event times = raw
`block_number`, kept in block units rather than wall-clock time for
consistency with D-A's block-based lag and because Ethereum block spacing is
close enough to uniform (~12s) not to warrant the added complexity of a
time-rescaled fit):

```
lambda(t) = mu + sum_{t_i < t} alpha * beta * exp(-beta * (t - t_i))
```

- `mu > 0`: background intensity.
- `0 < alpha < 1`: branching ratio (subcriticality required for a
  well-defined process).
- `beta > 0`: decay rate (1/beta = characteristic triggering timescale, in
  blocks).

**Fit:** MLE via the O(n) recursive log-likelihood (Ozaki 1979 / Ogata 1981
recursion for `R(i) = sum_{j<i} exp(-beta*(t_i - t_j))`), optimized with
L-BFGS-B over `(log mu, logit alpha, log beta)` to enforce the domain
constraints without a constrained optimizer. Observation window is
`[0, T]` with `T` = the last event's block number.

**Declustering (branching structure):** for each event `i` in time order,
compute the triggering score `alpha * beta * exp(-beta*(t_i - t_j))` against
every prior event `j` within a lookback of `20 / beta` blocks (captures
>99.9999% of the exponential kernel's mass; bounds the declustering pass to
O(n) rather than O(n^2)), plus the background score `mu`. Event `i`'s parent
is whichever candidate has the highest score. If the background wins, `i`
roots a new cluster; otherwise `i` joins its parent's cluster (parents are
always earlier in time and already resolved, so no union-find is needed —
cluster membership propagates forward in one pass).

**Episode filter:** clusters are analogous to D-A's candidate windows but
have no natural `w` — the timescale is recovered from the fit (`1/beta`), not
chosen. Apply the same `(k, theta)` grid ADR-001 fixed for D-A (`k in {5, 10,
20}`, `theta in {p99, p99.5, p99.9}` of the per-cluster total-USD
distribution), plus the same `num_accounts >= 2` breadth check — 9 grid
points total (no `w` axis). Primary point mirrors D-A's: `(k=10,
theta=p99.5)`. No generation-linking check: the branching structure already
*is* D-B's contagion signal, so a qualifying cluster is `>= k` positions that
the fitted process itself attributes to a shared branching chain, not an
independent per-position check.

### D-C: peaks-over-threshold (POT) on liquidation intensity

Reuses D-A's exact rolling-`w`-block anchor statistic (`total_usd` per
anchor) as the intensity measure — same `w` grid, same `theta` percentile
methodology — so D-C differs from D-A by exactly one thing: **no
generation-linking requirement**. This isolates the effect of D-A's
contagion criterion in E7 (a definition can differ from D-A in threshold
methodology, in contagion requirement, or both; D-C isolates "threshold
methodology only").

1. For each `w` in `{50, 100, 300}`: compute the per-anchor rolling-window
   `total_usd` (identical statistic D-A uses).
2. For each `theta` in `{p99, p99.5, p99.9}`: `theta_usd` = that percentile
   of the `total_usd` distribution (same methodology as D-A, recomputed per
   `w`).
3. **Peaks:** anchors with `total_usd >= theta_usd` are exceedances.
4. **Run declustering:** merge overlapping/adjacent exceedance windows into
   episodes (identical interval-merge D-A uses) — declustering run length is
   implicitly `w` blocks, consistent with the window statistic itself.
5. For each `k` in `{5, 10, 20}`: keep merged episodes with `num_positions
   >= k` and `num_accounts >= 2`. No generation/contagion check.

Full `(w, k, theta)` grid: 27 points, directly comparable to D-A's grid.

### Full severity measure

`severity_usd_total = total_liquidated_usd + bad_debt_usd`. Components:

- **`total_liquidated_usd`**: unchanged (sum of `amount_usd` across
  liquidations in the episode).
- **`bad_debt_usd`** (now computable — CAS-47's state-reconstruction engine
  exists): for every account with >= 1 liquidation inside the episode's
  `[start_block, end_block]`, reconstruct its position at `end_block`
  (`PositionStateEngine.positions_at`, batched via
  `engine.positions_at_many`) and price it with `PriceOracle`. Bad debt for
  that account is `max(0, debt_usd - collateral_usd)` — unbacked debt left
  after the cascade's liquidations, using raw USD exposure (not
  `liquidation_threshold`-weighted; that weighting is a health-factor
  concept, not a solvency one). Episode `bad_debt_usd` is the sum over
  touched accounts.
- **`price_impact_usd`**: still **not computable** — CAS-16 (DEX depth/
  liquidity ingestion) is Backlog on the Implementation Board as of this
  ADR; no DEX liquidity data has been ingested at all, so realized slippage
  cannot be honestly estimated from what's in `data/raw/`. This component is
  emitted as a null column, not a fabricated estimate, exactly as ADR-001's
  `severity_usd` was explicitly labeled a partial proxy pending this same
  dependency. **`severity_usd_total` excludes it and must be re-run once
  CAS-16 lands** — this is a scope limitation to flag on any figure that
  uses `severity_usd_total`, not a redefinition.

This severity computation is definition-agnostic: it runs against episodes
from D-A, D-B, or D-C without modification (same episode shape: `episode_id`,
`protocol_tag`, `start_block`, `end_block`).

## Findings (recorded post-implementation)

Running `compute_bad_debt` against the real Aave v2 ledger surfaced a
pre-existing data-quality issue in `PositionStateEngine`, not a bug in this
ticket's code: **~42% of (user, reserve) pairs replay to a negative final
`collateral_units` or `debt_units` balance** — a physically impossible state
(you cannot withdraw more than you deposited). One concrete instance: a
single account's ledger showed -37,907 WETH collateral alongside a $226M
cumulative debt balance that, left unguarded, would have dominated an
entire episode's bad-debt figure by itself. The most likely cause is
left-censored history (a Withdraw/Repay whose matching Deposit/Borrow
predates the ingested window) or one of the engine's other documented
simplifications (`state/engine.py`'s module docstring) — the same
unresolved family of issues behind CAS-28's 24-34% T2 HF-mismatch rate.
Fixing the engine's reconstruction is CAS-47's scope, not CAS-29's.

**Disposition:** `compute_bad_debt` excludes any touched account with a
negative raw balance on either leg from the bad-debt sum entirely (not just
clips the negative leg to zero — a negative balance signals the whole
reconstruction for that account is untrustworthy), and reports
`accounts_touched` / `accounts_excluded_data_quality` per episode so the
exclusion is visible rather than silently changing the number. On the
primary D-A grid point's three episodes, this excludes roughly 76-85% of
touched accounts; **`bad_debt_usd` is therefore a conservative lower bound
on true bad debt, not a full-population estimate** — any RQ1/RQ2 result
using it must state this, same as `price_impact_usd`'s exclusion. This is
treated as a validated empirical finding to flag to the mentor (Dr. Nilima
Dongre), not silently absorbed or parameter-tuned away, per this project's
pre-registration discipline (ADR-001's FTX non-detection is the precedent).

## Consequences

- E7 can now compare cascade probability/severity sensitivity across three
  independently-motivated definitions (D-A: fixed window + explicit
  contagion; D-B: data-driven branching-process clustering; D-C: pure
  intensity thresholding, no contagion requirement).
- `price_impact_usd` staying null is a known, tracked gap — any RQ1/RQ2
  result computed from `severity_usd_total` before CAS-16 lands must state
  that price impact is excluded.
- Any future change to the D-B/D-C parameters above, the lookback-window
  heuristic, or the bad-debt formula requires a superseding ADR.
