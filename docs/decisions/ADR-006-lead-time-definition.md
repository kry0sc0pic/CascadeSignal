# ADR-006: Lead-time (early-warning) definition

- **Status:** Proposed (pending mentor sign-off — adoption is a mentor decision,
  not a self-approval, per ADR-005)
- **Date:** 2026-08-15
- **Ticket:** CAS-46 (live monitor + paper reporting)
- **Depends on:** ADR-001 (cascade `start_block`/`end_block`, episode merge),
  the deployed operating point (`live/config.py`: threshold + `debounce_k`)
- **Blocks:** the Historical-tab card headline, paper Fig 3 lead annotations,
  `captions.md` — any figure or UI that states how far ahead the alarm warns

## Context

"Lead time" is the project's headline early-warning claim ("the alarm fires N
minutes before the cascade"), but it was never pinned down in an ADR. It lived
implicitly in one function — `scripts/live/build_historical_cascades.py::_lead()`
— which computes:

```
lead = half_cost_block − cross_block
```

where `cross_block` is the block the debounced alarm first fires and
`half_cost_block` is where cumulative liquidated USD in the window first reaches
50 % of the window total. That is a **cross → 50 %-of-dollars** measure, and it
was surfaced on the Historical card as the single "N min early warning" number.

The problem: `half_cost_block` lands in the *middle-to-late* part of the
cascade, so the reported number conflates two distinct quantities — genuine
advance warning (alarm before the cascade begins) and the cascade's own
internal burn time (how long after it starts before half the money is gone).
This **flatters short, fast cascades**. Measured on the four labelled major
cascades (deployed operating point, `debounce_k = 10`):

| Cascade | alarm → **start** (advance warning) | alarm → 50 %-cost (old "lead") | of which is burn time |
|---|---|---|---|
| Dec 2021 (v2) | **+1.0 min** | 14 min | 13 min |
| Jun 2022 (v2) | +10.2 min | 30 min | 20 min |
| May 2021 (v2) | +53.1 min | 76 min | 23 min |
| Feb 2025 (v3) | +192.3 min | 211 min | 19 min |

The Dec 2021 "14 min lead" is **1 min of real warning + 13 min of the cascade
burning its own money**. For Feb 2025 the number is honest (192 of 211 min is
genuinely before the crash starts), but a metric that is honest only for the
slow cascades is not a defensible headline. This ADR fixes the definition so
the early-warning claim cannot be read as inflated.

## Decision

Define two named, separately-reported quantities. All times are measured in
blocks and converted at 12.5 s/block for display.

**Anchors** (all per ADR-001 / the deployed operating point):
- `t_alarm` = `end_block` of the bar where the **debounced** alarm first fires:
  the `k`-th bar of a run of `k` consecutive bars with `n(t) ≥ threshold`, using
  the live `threshold` and `debounce_k` resolved exactly as `live/config.py`
  resolves them. Undefined if no such run completes at or before `t_end`.
- `t_start` = episode `start_block` (ADR-001 merged episode; equals the first
  liquidation of the episode).
- `t_end` = episode `end_block`.
- `t_half` = block at which cumulative liquidated USD inside the episode window
  first reaches 50 % of the window total.

Both are reported side by side — **neither replaces the other.** The existing
`time_to_half_cost` metric is validated and stays exactly as computed; this ADR
*adds* `advance_warning` next to it so the honest before-onset number is always
visible too.

**Metric 1 — advance warning (new):**
```
advance_warning = t_start − t_alarm
```
Positive ⇒ the alarm fired *before the cascade began*. This is the true
early-warning number; it must be shown wherever a lead is stated, labelled
"before cascade start."

**Metric 2 — time to half cost (existing, retained):**
```
time_to_half_cost = t_half − t_alarm
```
Kept unchanged (the current `lead_*` fields): it ties to the protectable-USD
result (Fig 5: how much value is still ahead of the alarm). It must be
**labelled "alarm → 50 % of USD," never "lead before the cascade."**

**Edge cases:**
- `t_alarm > t_start` (alarm fires during/after onset): `advance_warning ≤ 0` —
  reported as **"0 min (detected late)"**, not a negative headline. The alarm
  did not pre-warn.
- No qualifying run at or before `t_end`: **not detected** — no lead reported.

**Reporting commitment (mirrors ADR-005):** any figure, card, caption, or status
payload that states a lead **must show both numbers, each explicitly labelled**
— `advance_warning` as "before cascade start" and `time_to_half_cost` as
"alarm → 50 % of USD." Citing `time_to_half_cost` alone, or calling it "lead
before the cascade," is a scope violation of this ADR.

**Numbers, both metrics** (advance warning / alarm→50 %-cost): Dec 2021
**+1 / 14 min**, Jun 2022 **+10 / 30 min**, May 2021 **+53 / 76 min**, Feb 2025
**onset / 18 min** (see the v3 threshold amendment below — the +192 min reading
was a precursor-spike artifact; at the corrected 0.9998 threshold v3 fires at
onset).

### Amendment (2026-08-15): v3 threshold 0.99958 → 0.9998

Applying the advance-warning metric surfaced that v3's headline **+192 min** was
produced entirely by a **lone 20-bar n(t) spike ~192 min before onset** (peak
0.99981), an isolated precursor cluster with n(t) returning to baseline before
the cascade — not a sustained warning of *this* cascade. The v3 operating
threshold is therefore raised from its calibrated **0.99958** to **0.9998**
(`LIVE_AAVE_V3_THRESHOLD`), which cuts that spike (only 4 of its bars clear
0.9998, below the k=10 debounce). Measured across all v3 history, this is sound,
not a one-episode patch:

| threshold | full-history FAR | recall (45 D-A) | recall (primary) |
|---|---|---|---|
| 0.99958 (calibrated) | 0.42 / week | 1.00 (45/45) | 1/1 |
| **0.9998 (deployed)** | **0.17 / week** | 0.80 (36/45) | **1/1** |

FAR more than halves and the major cascade is still caught; the cost is 9 minor
episodes. Honest v3 result: **detected at onset (~0 advance) · 18 min to ½ cost**;
protectable-USD-in-time falls 70 %→16 % (v3 rarely fires strictly before onset),
though the Feb 2025 crash is still 79 % ahead of the alarm *within* the cascade.
This threshold change is frozen with this ADR; reverting or re-tuning it requires
a superseding ADR.

## Consequences

- `build_historical_cascades.py::_lead()` **adds** `advance_warning_*` while
  keeping the existing `lead_*` (=`time_to_half_cost`) fields unchanged; the
  Historical card and detail show both, each labelled. The precomputed
  `data/live_state/historical/*.json` must be rebuilt.
- Paper Fig 3 dual-reports both metrics; `captions.md` and Fig 1's crop
  annotations adopt the ADR-006 labels so "lead" is unambiguous — the first
  number is `advance_warning`, the second `time_to_half_cost`.
- The `mock2` deck leads its "signal in action" hero with **v2 May 2021**
  (+53 min advance), since after the v3 threshold amendment v3 no longer has a
  pre-onset lead; all v3 numbers (FAR, recall, protectable, lead) are restated
  to the 0.9998 operating point.
- The definition is frozen once this ADR is Accepted. Any change to the anchors,
  the 50 %-cost fraction, or the primary/secondary split requires a superseding
  ADR — same discipline as ADR-001/002/005.

## Alternatives considered (rejected)

- **Keep `cross → 50 %-cost` as the single "lead."** Rejected: it conflates
  advance warning with the cascade's own burn time and flatters fast cascades
  (Dec 2021's "14 min" is 1 min warning + 13 min burn). This is precisely the
  post-hoc-flattering reading ADR-001's discipline exists to prevent.
- **`cross → end_block`.** Rejected: even more inflated — it credits the alarm
  for the entire duration of the event it is supposed to precede.
- **`cross → start` only, drop the cost-timing metric.** Rejected: the
  alarm→50 %-cost view is the honest link to the economic result (how much USD
  is still ahead of the alarm, Fig 5). Keep it — just label it truthfully and
  demote it from the headline.
- **`cross → first liquidation` where "first liquidation" is any liq in the
  padded display window, not the episode start.** Rejected: the padded window
  is a display convenience; the ADR-001 `start_block` is the pre-registered
  onset and the only defensible zero-point.
