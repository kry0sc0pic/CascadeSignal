# ADR-001: Pre-registered cascade definition

- **Status:** Accepted
- **Date:** 2026-07-05
- **Ticket:** CAS-15 (Mock 1 · P0)
- **Depends on:** CAS-4/5 (ingestion)
- **Blocks:** CAS-16 (labeler), CAS-18 (T3), CAS-19 (atlas), CAS-20 (feature bars), CAS-24/26 (baselines/Hawkes), CAS-35 (splitter) — no model or feature-selection process may see cascade labels before this ADR is committed and timestamped.
- **Mirror:** Notion Decisions Log, [Mini Project II](https://app.notion.com/p/38a4a5891c64807d9a1bd49542a1689d)

## Context

RQ1/RQ2 forecast and explain *cascades*, not individual liquidations. Because
"cascade" has no canonical on-chain definition, an adversarial reading of this
project is that the definition was tuned post-hoc to make the model look good.
The mitigation is pre-registration: commit the exact detection algorithm,
parameter grid, and severity measure in this ADR, timestamp it in Notion,
**before** any labels are computed and before any model or feature-engineering
process is allowed to see them. PLAN.md §5 sets the scope; this ADR fixes the
implementation so CAS-16 has no undocumented degrees of freedom.

## Decision

### Primary definition — D-A (rolling window + generation-linking)

A rolling `w`-block window, anchored at the block of a liquidation event, is a
**cascade** iff all three hold:

1. **Severity:** total `amount_usd` liquidated inside the window ≥ `θ_USD`,
   where `θ_USD` is the empirical percentile `θ` of the distribution of
   window-volume across *every* w-block window anchored at a liquidation event
   over the full study period (not just candidate windows) — i.e. the
   threshold is data-driven per grid point, recomputed for each `w`.
2. **Breadth:** ≥ `k` distinct positions, identified as distinct
   `(user, collateral_asset, debt_asset)` tuples, spanning ≥ 2 distinct
   `user` accounts.
3. **Contagion (generation-linking):** ≥ 2 "generations" of liquidations
   linked via price impact on a shared collateral asset (algorithm below).

### Generation-linking algorithm

Direct order-book / DEX-depth price-impact data is not available at Mock 1
(DEX depth ingestion is CAS-8, Mock 2). D-A therefore uses liquidation
clustering on a shared collateral asset as an observable proxy for price
impact propagation:

1. Within the candidate window, group liquidation events by `collateral_asset`.
2. Within each asset group, sort events by `block_number`.
3. Partition each asset's sorted events into **waves**: start a new wave
   whenever the gap to the previous event (in blocks) exceeds
   `generation_lag_blocks` (fixed at **20 blocks**, ≈ 4 minutes at Ethereum's
   ~12s block time — the assumed propagation time for a liquidation's
   collateral sale to move price enough to trigger the next liquidation).
   Consecutive events within the lag form the same wave/generation.
4. The number of generations for that asset in the window = number of waves.
5. The window's generation count = `max` over assets present in the window.

`generation_lag_blocks` is a fixed constant, not part of the grid — it models
a physical propagation-time assumption independent of the detection window
length `w`. Rationale for the value and any revision must go through a new
ADR, not a silent code change.

**Interpretation:** a single burst of liquidations on one collateral asset is
1 generation (correlated, not necessarily contagious). A burst, a gap, then a
*second* burst on the same asset is 2 generations — consistent with "the
first liquidation's collateral dump moved the price enough to trigger more
liquidations after some delay."

### Merging into episodes

Overlapping or adjacent qualifying windows are merged into a single episode
(`start_block` = min window start, `end_block` = max window end). Each episode
records: protocol(s), block/time bounds, total liquidated USD, distinct
position/account counts, max generation count, and the grid point `(w, k, θ)`
that produced it.

### Parameter grid

| Parameter | Values | Primary |
|---|---|---|
| `w` (window, blocks) | {50, 100, 300} | 100 |
| `k` (min positions) | {5, 10, 20} | 10 |
| `θ` (severity percentile) | {p99, p99.5, p99.9} | p99.5 |

27 grid points total; primary = `(w=100, k=10, θ=p99.5)`.

### Severity measure

Full severity (PLAN §5) = USD liquidated + realized price impact + bad debt
created. At Mock 1, only USD liquidated is computable — realized price impact
needs DEX depth (CAS-8, Mock 2) and bad debt needs position-state
reconstruction (CAS-13, Mock 2). **Mock 1 severity = `total_liquidated_usd`
only**, explicitly labeled as a proxy pending CAS-17. This is a scope
limitation, not a redefinition: E7 (definition-sensitivity) and CAS-17 must
re-run once the full measure is available, and figures must state which
severity measure they use.

### Alternative definitions (not implemented in CAS-16)

- **D-B:** Hawkes declustering — cluster membership via the branching
  structure recovered from a fitted Hawkes process (depends on M1 / CAS-26).
- **D-C:** Peaks-over-threshold (POT) on liquidation intensity.

Both are scoped to CAS-17 (deferred past Mock 1); D-A is the only definition
gating Mock 1 modeling.

### Scope

Mock 1 labeling runs on **Aave v2 liquidations only** (per the Mock 1 refined
goal — Aave v3 core events are deferred past Mock 1). The labeler
(`src/cascadesignal/labels/cascade_labeler.py`) is protocol-parameterized so
Mock 2 can extend to Aave v3/Compound/Maker without an ADR change, since doing
so does not alter the definition, only its input set.

## Acceptance criteria (CAS-15)

- [x] ADR-001 merged to `docs/decisions/`.
- [ ] Mirrored + timestamped in Notion Decisions Log before modeling.
- [x] Severity definition (USD now; price impact + bad debt deferred to CAS-17) recorded.
- [ ] Circulated to mentor (Dr. Nilima Dongre) for sign-off.

## Findings (recorded post-implementation, pre-modeling)

Running D-A exactly as specified above (Aave v2 liquidations, full grid,
generation-lag fixed) against the ingested 2021-01 → 2026-02 dataset:

- **China (2021-05-19)** labels as a cascade at 21/27 grid points; it misses
  only the strictest severity bar (`θ=p99.9`) at the two shortest windows
  (`w=50`, `w=100`) — its ~$77M in a single hour clears p99.9 once the window
  widens to `w=300` but not at shorter windows. p99.9 at `w=50`/`w=100` is set
  by the 2022-06-14 stETH-depeg spike within our own Terra-window episode
  (up to $48.4M in a single 100-block window) — a more concentrated event
  than China, not (as an earlier draft of this section incorrectly guessed)
  Oct'25/Feb'26, which turn out to be barely present in Aave v2 data at all
  (see below).
- **Terra (2022-05 → 2022-06)** labels as a cascade at 24/27 grid points, with
  generation counts of 3–5.
- **FTX (2022-11-06 → 2022-11-11) does not label as a cascade at any of the
  27 grid points**, even when the input is widened to all five Mock-1
  protocols combined (Aave v2/v3, Compound v2/v3, Maker). The maximum
  liquidated USD in any 100-block window during that period is ~$7.6M,
  versus a p99.5 severity threshold of ~$30M at `w=100` — set by China and
  the 2021-12-04 episode themselves, both of which cluster right around that
  level — FTX's on-chain liquidation activity (324 events over 5 days) was
  real but diffuse, never spiking.
- **Oct 2025 and Feb 2026 do not label as cascades on Aave v2 at any grid
  point** — but for a different reason than FTX. By late 2025, Aave v2
  mainnet activity is negligible (Oct'25: $151K/346 events; Feb'26:
  $1.6M/409 events, vs. published platform-wide figures of $250M+ and $429M
  respectively) — liquidity had migrated to v3. Re-running D-A directly on
  **Aave v3 mainnet liquidations** (already ingested; only v3 core events are
  deferred past Mock 1) recovers **Oct 2025** at 12/27 grid points
  ($116.4M peak). **Feb 2026 still does not clear**, even on v3: its largest
  100-block window ($77.8M) falls short of v3's own p99.5 threshold
  ($117.4M, set by an even larger 2025-02-03 event in v3 data). The most
  likely explanation is that the published $429M figure aggregates across
  **all chains** (Aave v3 is deployed on multiple L2s per PLAN §3), which
  Ethereum-mainnet-only data structurally cannot reconstruct. See
  `docs/episodes/oct-2025.md` and `docs/episodes/feb-2026.md`.

FTX's non-detection is treated as a **substantive empirical result, not a
defect**: it is consistent with FTX's collapse being predominantly a
CeFi-contagion event, unlike Terra which was DeFi-native. Per
pre-registration discipline, D-A's parameters are **not** retroactively
loosened to force FTX to clear — doing so after observing the outcome would
be exactly the kind of post-hoc fitting this ADR exists to prevent.

Oct'25/Feb'26's non-detection on Aave v2 is a **different kind of gap** —
data availability/scope, not a statement about whether a cascade occurred.
The atlas (CAS-19) stays Aave-v2-only per Mock 1's stated scope; folding in
v3 and L2 chains is tracked as CAS-44 follow-up, not solved here.

**Disposition:** T3 (CAS-18) asserts China and Terra label as cascades across
the full grid. FTX is asserted only to be *reconstructable* (the liquidation
event data for that window loads and is queryable — satisfies the "always
reconstructable/labelable" project scope note) — it is documented as a
validated non-cascade under D-A on Aave v2 data, not force-fit. Revisit via
CAS-17 (D-B Hawkes declustering / D-C POT, which use intensity/rate rather
than a fixed severity threshold and may be more sensitive to FTX's sustained-
but-lower-amplitude pattern) and flag to the mentor (Dr. Nilima Dongre) for
sign-off alongside the rest of this ADR.

## Consequences

- CAS-16 has a fully specified algorithm with no hidden parameters —
  `generation_lag_blocks`, the grid, and the percentile-threshold
  methodology are all fixed here.
- T3 (golden-label test) asserts China/Terra/FTX label as cascades at the
  primary point and across the full grid, using this exact algorithm.
- Any future change to the generation-linking lag, the grid, or the severity
  formula requires a superseding ADR, not a silent code edit — this is the
  pre-registration guarantee E7 relies on.
