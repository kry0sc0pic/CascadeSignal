# ADR-008: Tolerant debounce (k-within-window persistence filter)

- **Status:** Proposed (pending mentor sign-off, per ADR-005)
- **Date:** 2026-08-16
- **Ticket:** CAS-46 (live monitor + paper reporting)
- **Depends on:** the deployed operating point (`live/config.py`), ADR-006
  (lead-time definition), ADR-007 (USD-marked blend)
- **Blocks:** any figure/card/payload that states a lead or FAR

## Context

The alarm applies a persistence filter to the branching-ratio crossings: raise
one alert only after a sustained run above threshold, to suppress isolated
single-bar spikes. The original filter required **`k` STRICTLY CONSECUTIVE**
above-threshold bars (deployed `k = 10`).

Measured against the v2 major cascades, strict consecutiveness has two coupled
failure modes on a **flickering** near-critical run-up (n(t) hovering right at
the threshold, dipping in and out):

1. **Fires late.** Every dip resets the consecutive counter to zero, so the run
   of 10 doesn't complete until long after the first crossing.
2. **Spawns extra false alarms.** Each dip-and-recovery is counted as a fresh
   distinct alarm.

Concretely (v2, threshold 0.99900):

| cascade | first crossing | strict-k=10 fire | debounce cost | dips in between |
|---|---|---|---|---|
| May 2021 | +70.2 | +63.8 | 6.5 min | 18 |
| Dec 2021 | +12.5 | +3.8 | 8.8 min | 13 |
| Jun 2022 | +128.5 | +6.0 | **122.5 min** | **569** in 588 bars |

The bar width is already the 1-block floor (~12.5 s; the pure `k·bar` delay is
only ~2 min) — the lost lead is the *consecutiveness*, not the bar duration.

## Decision

Fire once **`k` of the last `W` bars are above threshold** (a rising edge of
that condition), instead of `k` strictly consecutive. `W = k` reproduces the old
behaviour; `W > k` tolerates dips. Deployed: **`k = 10`, `W = 100`**
(`LIVE_<P>_DEBOUNCE_K` / `LIVE_<P>_DEBOUNCE_W`; default `W` =
`DEFAULT_DEBOUNCE_WINDOW = 100`).

```
above_i = n(t)_i >= threshold
fire    = rising edge of ( count(above over last W bars) >= k )
```

This helps on *both* axes at once, because the window both (a) lets a flickering
run reach `k` sooner and (b) keeps the alarm latched through dips instead of
re-triggering on each recovery.

## Evidence (deployed operating point, both chains)

| chain | FAR strict k=10 | FAR tolerant k=10/W=100 | recall | major leads |
|---|---|---|---|---|
| Aave v2 | 0.51/wk | **0.38/wk** | 98/98 | May +65 · Dec **+10** (was +4) · Jun +7 |
| Aave v3 | 0.19/wk | **0.16/wk** | 45/45 | Feb onset (−1), unchanged |

FAR **drops** on both chains, recall stays 100%, Dec 2021's lead more than
doubles, and v3 is unaffected (its problem is the absence of a pre-onset signal,
not a flickering one — the window has nothing earlier to fire on). A strict
win, not a FAR/lead trade.

The window is deliberately **not** pushed to recover Jun 2022's early +128
(that first crossing is the alarm firing on multi-day June turbulence hours
before the labelled onset — kept out by staying at `k=10` where Jun reports the
clean +7). `k=5/W=100` would surface the +128 at FAR 0.40 if ever wanted.

## Consequences

- `live/config.py` gains `debounce_window` (`LIVE_<P>_DEBOUNCE_W`, default 100).
- The debounce is implemented tolerantly in every "scores → fires" path:
  `scripts/paper/_common.debounced_fire_mask`, `live/monitor.py` (streaming, a
  `deque(maxlen=W)` of recent above-flags), and
  `scripts/live/build_historical_cascades._first_alarm_block`; payloads carry
  `debounce_window`. Figs 3/4/5 + all mock2 figures + historical payloads
  regenerated.
- The fit `(μ, α, β, s)` is unchanged — the debounce only changes the alarm
  decision, so no re-bootstrap/re-calibration is needed.
- Frozen once Accepted; changing `k`, `W`, or the filter shape needs a
  superseding ADR.

## Alternatives considered (rejected)

- **Lower strict `k`.** Recovers some lead but raises FAR across the board
  (k=5 → 0.64/wk on v2) — the window achieves lower FAR *and* earlier fire.
- **Leaky-bucket counter** (increment above, decrement below, fire at `k`).
  Similar effect, but the fixed sliding window is simpler to reason about and to
  vectorize for the figure sweeps.
- **Widen the feature bar.** Would only coarsen the signal and discard lead; the
  bar is already at the 1-block floor.
