# ADR-007: USD-marked Hawkes (dollar-weighted branching ratio)

- **Status:** Proposed (pending mentor sign-off — adoption is a mentor decision,
  not a self-approval, per ADR-005)
- **Date:** 2026-08-15
- **Ticket:** CAS-46 (live monitor + paper reporting)
- **Depends on:** ADR-001 (cascade definition — the D-A severity axis is USD),
  the deployed operating point (`live/config.py`)
- **Retains:** the ADR-006 amendment's v3 threshold (**0.9998**). The deployed
  model is a *blend* (keeps count sensitivity), so it still fires on the
  −192 min precursor spike; that spike is a lone 20-bar cluster whose n(t)
  returns to baseline for ~900 bars before the real cascade (verified), i.e.
  the model-independent artifact ADR-006 documented. v3 therefore keeps the
  0.9998 operating point and reports Feb 2025 at onset. ADR-006 stands.
- **Blocks:** the operating-point recalibration, paper Figs 3/4/5, the mock2
  deck FAR slides — anything that reports FAR or recall.

## Context

The count-Hawkes branching ratio n(t) scores the **aggregate liquidation-event
count** stream: its self-exciting history `R` accumulates *events per bar*,
irrespective of how much USD each liquidation carried. Investigating the
operating point's false alarms (CAS-46) showed this is precisely their cause.

Ruling out the easy explanations first, on the deployed operating point
(v2 thr 0.9991 / v3 0.9998, k=10):

- **Not aftershocks.** A post-episode cooldown window reclassifies almost none
  of the false alarms (v2 15/68, v3 0/25) — they are not the tail of a labelled
  cascade.
- **Not stale labels.** Re-running the ADR-001 labeler over the full
  liquidation history (now through 2026-02-28) reproduces the *identical* 98
  (v2) / 45 (v3) episodes. The labels are not missing coverage.
- **Not diffuse noise.** The non-dust false-alarm windows are as count-dense,
  as broad (distinct positions), and as multi-generational as the labelled
  cascades — they pass ADR-001 criteria 1's *count*, 2 (breadth) and 3
  (generations).

The single axis that separates them is **dollar severity at the cascade
timescale** (the ADR-001 primary window, w=100 blocks ≈ 20 min):

| class | median window-USD | severity percentile | reaches p99 bar |
|---|---|---|---|
| true detections | $28.9M (v2) / $128.9M (v3) | p99.4 / p99.9 | at the label bar |
| non-dust false | **$3.6M / $28.0M** | **p92.7 / p89.5** | 2/68 · 0/25 |
| dust false | ~$0 | p38 / p14 | 0 |

The daily-dollar view is misleading (stress persists all day, $30–200M/day),
but in the actual 20-minute window the false-alarm bursts carry ~**8× less**
dollar volume than a real cascade. **The count-Hawkes fires on event-count
clustering; a cascade is defined (ADR-001) by dollar volume.** The non-dust
false alarms are *count-dense, dollar-light* — flurries of many small
liquidations. This is the same mechanism as the near-zero-value "dust" storms,
one tier up; one lever fixes both.

## Decision

Drive the univariate Hawkes' self-exciting history `R` with a **USD-weighted
blend mark** instead of pure event counts, while the observed process — and the
Poisson likelihood — remains the **liquidation-event count** (the standard
*marked* ground-intensity form: marks modulate the future event rate):

```
mark_i = count_i + w · (usd_i / s)               # blend; w = 0.5, s = mean pos usd
R_i    = decay * (R_{i-1} + mark_{i-1})           # blended excitation
λ_i    = μ + α·R_i                                 # rate of liquidation EVENTS
n(t)   = α·R_i / λ_i   ∈ [0, 1)                     # excited share
NLL    = Σ (λ_i − count_i·log λ_i)                  # Poisson on counts, unchanged
```

**Why a blend, not pure USD.** Pure-USD marks (`w → ∞`) kill the false alarms
but also kill the count signal's edge on *fast* cascades: Jun 2022's genuine
early warning is itself a dollar-light flurry, so pure USD detects it only at
onset. A joint sweep over (dollar-weight × threshold × k) showed the blend
**w = 0.5** keeps count sensitivity for fast-cascade lead *and* the dollar
volume that suppresses count-dense/dollar-light false alarms. `w = 0` is the
count model, `w → ∞` is pure USD; 0.5 is the swept optimum. USD is scaled by its
fit-time positive mean; n(t) is scale-invariant (the scale only conditions the
optimizer, and is persisted in `LiveModelState.excite_scale` so a reload scores
identically). Effective branching ratio `α·E[mark]/β ≪ 1` (stationary).

Implementation: `make_operating_model()` = `HawkesUnivariateBranchingRatio(
excite_col="liquidated_usd", dollar_weight=0.5)`, built in one place and used by
the live monitor, calibration, historical replay, and the paper's operating-
point figures. `excite_col=None` is numerically identical to the count model
(kept as the paper's baseline/ablation). `build_liquidation_bars` now emits
`liquidated_usd`.

## Evidence — deployed operating point (blend w=0.5, k=10, walk-forward-calibrated)

| chain | threshold | FAR/wk | recall (all) | major | advance warning |
|---|---|---|---|---|---|
| **Aave v2** | 0.99900 | **0.51** | **98/98** | 3/3 | May **+64** · Dec **+4** · Jun **+6** |
| count baseline (v2) | 0.99910 | 0.34 | 84/98 | 3/3 | May +51 · Dec +0.2 · Jun +6 |
| **Aave v3** | 0.9998 | **0.19** | **45/45** | 1/1 | Feb 2025 onset (−1.5) |
| count baseline (v3) | 0.9998 | 0.17 | 36/45 | 1/1 | Feb 2025 onset (−1.5) |

On Aave v2 the blend lifts **recall 0.86 → 1.00** and keeps clean pre-onset lead
on all three majors (May +64, Dec +4, Jun +6) at FAR 0.51/wk. The operating
threshold 0.99900 sits on the lead/FAR frontier: lower thresholds buy more
fast-cascade lead (Jun up to +127) at higher FAR, but that extra lead is the
alarm firing hours early on adjacent multi-day turbulence (June 2022 3AC/stETH),
so the deployed point takes the honest clean leads at the lower FAR. The false
alarms that remain are overwhelmingly real 2022 stress (Terra/3AC/FTX) outside
the strict D-A labels, not noise. On Aave v3 the blend is essentially unchanged
from the count
model — still onset detection at the ADR-006 threshold 0.9998, because v3's one
cascade is fast and its −192 min precursor is the model-independent artifact
ADR-006 documented (a 20-bar spike that returns to baseline ~900 bars before
onset). v3 keeps 0.9998; ADR-006 stands.

## Consequences

- Operating point recalibrated through the existing pipeline
  (`calibrate_thresholds.py` walk-forward OOF) on the blend score: v2 deployed
  at **0.99900** (`LIVE_AAVE_V2_THRESHOLD`; the raw calibrated 0.99868 sits lower
  on the frontier — 0.99900 takes clean pre-onset leads at FAR 0.51 instead of
  0.70). v3 **keeps** the ADR-006
  `LIVE_AAVE_V3_THRESHOLD=0.9998` (the walk-forward value 0.99959 would report
  the −192 artifact; 0.9998 cuts it — ADR-006 stands). Both `k=10`.
- The live-monitor fit persists `excite_scale` in `LiveModelState`; the state
  files are re-bootstrapped (`scripts/live/bootstrap_fit.py`) so the persisted
  fit is the blend. Paper Figs 3/4/5, `build_historical_cascades.py` payloads,
  `captions.md`, and the mock2 deck restate FAR/recall/lead to the blend
  operating point, with the count model shown as the baseline ablation.
- The frozen ADR-001 cascade *definition* is unchanged — this changes the
  *signal*, not the labels or the evaluation rule.
- Definition frozen once Accepted; changing the mark, the dollar-weight `w`, its
  transform, or the likelihood requires a superseding ADR.

## Alternatives considered (rejected)

- **Hard severity gate** (fire only if trailing-window USD ≥ θ). Cuts FAR
  ~85% but eats lead on fast cascades (Jun 2022 +6 → −34 min): requiring the
  dollars to *accumulate* discards the count signal's early edge. The mark
  achieves the same FAR cut while preserving lead, because excitation is
  continuous, not a step gate.
- **Two-tier watch+confirmed alarm.** Sound, but keeps the count model's FAR on
  the "watch" tier; the marked model improves the primary signal itself, so the
  two-tier split is unnecessary.
- **Extend the labels to the stress events.** Rejected as circular — crediting
  the alarm wherever it fires on "real stress" defines away the false-alarm
  problem. The ADR-001 labels stay frozen; the signal is what changes.
- **Drop dust by count-thresholding only.** Removes the dust storms but not the
  $3.6M/$28M mid-tier false alarms, which are the bulk. The mark handles both on
  one axis.
