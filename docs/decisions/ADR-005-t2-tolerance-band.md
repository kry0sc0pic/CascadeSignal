# ADR-005: Redefine the T2 gate as a tolerance band

- **Status:** Accepted (mentor sign-off 2026-07-23)
- **Date:** 2026-07-23
- **Ticket:** CAS-28
- **Depends on:** PLAN.md §8 (original T2 spec)
- **Blocks:** CAS-31 (snapshot materialization) → CAS-8 (GNN) → CAS-15 (E4, the
  RQ2 read) — the critical path is currently gated on T2 passing.

## Context

PLAN.md §8 pre-registers the T2 test as an exact-boundary check: "every
observed liquidation must have reconstructed HF < 1 at trigger; > 2% mismatch
blocks the pipeline." No tolerance was specified — a reconstructed HF of
1.000001 counts as a mismatch exactly like a reconstructed HF of 5.0.

CAS-28 spent sixteen measured levers driving the mismatch rate down under
that exact-boundary reading:

| Step | Fix | Rate after |
|---|---|---|
| (baseline) | gateway misattribution (masking a worse bug) | 28.1% (artifact) |
| — | onBehalfOf + Withdraw re-attribution | **69.2%** (honest regression — the fix corrected ledger attribution and unmasked a bug the artifact had been hiding) |
| Track B | per-reserve collateral-toggle | 68.2% |
| Track C | naked aToken-transfer correction | 53.9% |
| Track D+E | interest-index accrual (scaledBalance) | 37.8% |
| Lever 1 | pre-liquidation HF convention (gate metric) | 30.9% |
| Lever 2 | Chainlink-primary (blended) price oracle | 24.2% |
| Lever 3 | point-in-time liquidation thresholds | 19.0% |
| Lever 4 | launch-month core-event backfill (completeness fix) | **19.7%** (honest regression — filling in missing history can only ever reveal previously-masked mismatches) |
| Lever 5 | ETH-numéraire pricing (H1) | 16.4% |
| Lever 6 | full-history ETH feeds + unbounded staleness (H2) | 11.4% |
| Lever 7 | same-block cascade state ordering (H4a) | 11.3% |
| Lever 8 | compound interest index to the trigger second (H5) | 10.8% |
| Lever 9 | exact token-level ledger (H3) | 8.4% |
| Lever 10 | same-block price ordering (H4b) + earlier-activity generalization | 8.3% |
| Lever 11 | real oracle-source timeline (early-2021 gap + stETH/CVX) | 7.6% |
| Lever 11b | GUSD/ENS/LUSD custom-adapter cross-rate replication | 6.5% |
| Lever 11c | xSUSHI SushiBar share-price replication | 6.0% |
| Lever 12 | live `AaveOracle.getAssetPrice` fallback for `unexplained` mismatches | 2.6% |
| Final extension | Lever 3/Track D config+index pulls extended past the prior 24.5M cap | **2.5853%** (1,247 / 48,235 measurable, 97.8% coverage) |

Two attempted fixes were measured and reverted rather than kept on faith:
wiring the 2024/2025 stablecoin-migration addresses (8.29% → 13.36%, reverted
— the new sources are live-computation wrappers with no event history, and
clipping the old ones with nothing real to replace them broke ETH-numeraire
coverage for every touched position) and clipping WBTC's pre-2023 feed at its
2023 switch block (8.38% → 9.26% in isolation, reverted — Aave's move to a
wrapper is a risk-overlay, not a materially different price absent a de-peg).

Every mechanism this ticket's two ranked hypothesis catalogs named has now
either **landed** (the eighteen rows above), been **refuted**, or been
**ground-truth-confirmed as boundary noise**:

- **Refuted:** the wind-down hypothesis (suspected stepped LT cuts near the
  study-period tail) — extending `backfill_reserve_config_history.py` and
  `backfill_reserve_index.py` past their prior 24.5M-block cap found 0 new
  `CollateralConfigurationChanged` rows (nothing to miss) and +874
  `ReserveDataUpdated` rows that fixed only 2 of 1,249 mismatches.
- **Ground-truth-confirmed as boundary noise:**
  - **H7** (archive-node `getUserAccountData`, n=300 sample of the
    pre-Lever-12 `unexplained` bucket): 76.8% of true disagreements have
    on-chain HF ∈ [0.99, 1) — Aave's own live contract, not another
    reconstruction; debt-leg diff median −0.97%, collateral-leg diff median
    0.00%.
  - The debt-formula audit against Aave v2's real deployed `MathUtils.sol`
    (via `getsourcecode`): `calculate_linear_interest`/
    `calculate_compounded_interest` match term-for-term, ruling out a
    compounding-formula bug as the source of H7's debt gap.
  - **H8** (Messari subgraph independent ledger cross-check, n=40): a third,
    independently-coded data source confirms the collateral-side
    reconstruction (median diff 0.02%); the debt-leg comparison is noisy but
    traced to the subgraph's own sparse snapshotting, not this project's
    reconstruction.
  - **Final 10-case hand-trace** (2026-07-23, closest-to-boundary of the
    2024+ residual, same technique as H7): 8/10 directly confirmed by Aave's
    own on-chain contract (on-chain HF ≥ 1, agreeing to 5–8 decimal places —
    e.g. reconstructed 1.000000 vs. on-chain 1.0000000441584274); the other 2
    disagreements were themselves within parts-per-10-million of the
    boundary (on-chain HF 0.9999997 and 0.99999998).

The residual is now close-margin and boundary-concentrated, not
outlier-driven: 40% of the 1,247 remaining mismatches sit within 0.5% of
HF = 1, 58% within 1%, 68% within 2%. The exact-boundary gate at this point is
measuring instrument precision — the accumulated sub-1% error of reconstructing
exact-block HF from event logs, ledger replay, and nearest-prior price
resolution — not reconstruction correctness. The only remaining path to close
CAS-28 under the exhausted-mechanism catalog is H9: pre-register a
physically-justified tolerance band. Because this changes a pre-registered
gate spec (PLAN.md §8), it requires an ADR with mentor sign-off, per this
project's pre-registration discipline (ADR-001) — this document is the
proposal; adoption is a mentor decision, not a self-approval.

## Decision

Redefine the T2 mismatch condition from `reconstructed HF >= 1` to
`reconstructed HF >= 1 + ε`, with **ε = 0.01** (1%).

Measured tolerance curve (current data, 48,235 measurable):

| ε | mismatches | rate | | ε | mismatches | rate |
|---|---|---|---|---|---|---|
| 0 (exact) | 1,247 | 2.585% | | 0.75% | 634 | 1.314% |
| 0.25% | 911 | 1.889% ✅ | | 1.00% | 527 | **1.093%** ✅ |
| 0.50% | 744 | 1.542% ✅ | | 2.00% | 397 | 0.823% ✅ |

The gate passes for **every** ε ≥ 0.25% — the conclusion (T2 passes) is
insensitive to the exact choice within this range. **ε = 1% was deliberately
not chosen as the minimum ε that passes** (that would be 0.25%); it was chosen
because it is independently justified as this reconstruction's actual measured
precision, not tuned to clear the 2% bar with minimum margin:

1. **H7's component diffs put the reconstruction's precision at ~1%.** Debt-leg
   diff median −0.97% against Aave's own live `getUserAccountData` (collateral
   is exact); 76.8% of true disagreements concentrate at on-chain HF ∈ [0.99,
   1) — i.e. within 1% of the boundary the reconstruction's own error already
   places most of the residual.
2. **Chainlink's own deviation thresholds bound the achievable price fidelity
   at a comparable scale.** The feeds this project prices with use vendor
   deviation thresholds in the 0.5–2% range (tighter for stablecoin pairs,
   looser for major-crypto pairs) — Aave's on-chain liquidation logic itself
   only ever sees a price that has moved by at least that much since the last
   update, so no reconstruction sourced from the same feeds can be more
   precise than that band allows.

Both are independent, pre-existing measurements — neither was computed by
searching for a value that clears 2%.

**Reporting commitment:** the T2 gate output and `t2_mismatch_report.py` must
always report **both** numbers going forward — the exact-boundary rate
(2.5853%, still failing) and the tolerance-band rate (1.093% at ε=1%,
passing) — so neither figure is presented without the other.

## Consequences

- The T2 gate passes at 1.093% (ε=1%) against the 2% bar; CAS-28 closes.
- The critical path unblocks: CAS-31 (snapshot materialization) → CAS-8 (GNN)
  → CAS-15 (E4, the RQ2 read) can proceed.
- The full 53.9% → 2.5853% lever arc, the H7/H8/hand-trace ground-truth
  methodology, and this precision analysis become paper §5 (data quality)
  material (CAS-42) — a real methodological result regardless of where this
  ADR lands.
- ε = 1% is frozen once this ADR is Accepted. Any future change to ε, or to
  the tolerance-band mechanism itself, requires a superseding ADR — same
  discipline as ADR-002's D-B/D-C parameters.
- Both the exact-boundary and tolerance-band rates must be reported together
  on every future run of the gate and the report script; a result that cites
  only one number is a scope violation of this ADR.

## Alternatives considered (rejected)

- **Keep the exact-boundary gate.** Rejected: sixteen levers and three
  independent ground-truth checks (H7, the `MathUtils` audit, H8) now show the
  residual is instrument precision, not reconstruction error — an exact
  boundary at this point measures how precisely event logs can reconstruct a
  continuously-evaluated on-chain quantity, not whether the reconstruction is
  correct.
- **Accept the gate failing and proceed anyway.** Rejected: this leaves a
  pre-registered gate permanently meaningless (any future regression would
  also silently fail to block the pipeline), and abandons the pre-registration
  discipline this project holds itself to elsewhere (ADR-001, ADR-002).
- **A dynamic, per-position ε derived from each position's own feed deviation
  thresholds.** Rejected for now: more physically precise in principle (a
  position's tolerance would track the actual feeds it's priced with rather
  than one global constant), but makes the gate's pass/fail definition
  data-dependent on a per-position basis, and adds real implementation
  complexity for a residual that a single global ε already explains well
  enough to pass at every ε ≥ 0.25%. Noted as a future robustness check, not
  adopted here.
