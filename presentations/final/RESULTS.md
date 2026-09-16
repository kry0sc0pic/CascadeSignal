# CascadeSignal — Final Results

Deployed operating point: USD-marked blend Hawkes `n(t) = count + 0.5·usd`
(ADR-007), tolerant debounce `k = 10` within window `W = 100` (ADR-008).
Thresholds: Aave v2 `0.99900`, Aave v3 `0.99980`.

## Headline
- **4 / 4 major cascades caught**; **100% episode recall** on both chains
  (v2 98/98, v3 45/45).
- Both chains well under the 1-false-alarm/week budget.
- Every major Aave v2 cascade warned **before onset**.

## Per chain

| chain | threshold | FAR (full) | FAR (out-of-sample) | recall all | recall major | protectable USD |
|---|---|---|---|---|---|---|
| Aave v2 | 0.99900 | **0.38/wk** | **0.14/wk** | 98/98 | 3/3 | 81% ($3.34B / $4.11B) |
| Aave v3 | 0.99980 | **0.16/wk** | **0.18/wk** | 45/45 | 1/1 | 22% ($1.27B / $5.77B) |

## Per major cascade

| cascade | chain | advance warning | to 10% of USD | to 50% of USD | liquidated (window) | peak n(t) |
|---|---|---|---|---|---|---|
| May 2021 | v2 | **+65 min** | — | 86 min | $78M | 0.9999 |
| Dec 2021 | v2 | **+10 min** | — | 23 min | $33M | 0.9999 |
| Jun 2022 (Terra/3AC) | v2 | **+7 min** | — | 26 min | $49M | 0.9999 |
| Feb 2025 | v3 | onset (−1 min) | — | 18 min | $147M | 1.0000 |

## Lead across all 143 episodes (median)

| metric | Aave v2 (98) | Aave v3 (45) | All 143 |
|---|---|---|---|
| advance warning (→ onset) | +9.6 | −3.8 | **+6.5 min** |
| alarm → 10% of USD | +18.3 | −0.6 | **+10.2 min** |
| alarm → 50% of USD | +26.2 | +17.7 | **+19.0 min** |
| % warned before onset | 77% | 27% | **61%** |

Median event is caught **~6 min before onset**, **~10 min before 10%** of its
dollar damage, **~19 min before half**. v2 carries the lead (fast v3 minor
episodes are detected at onset — structural, not tunable).

## vs the count-Hawkes baseline (what the two upgrades bought)
- **USD-marked blend** (ADR-007): recall v2 0.86→1.00, v3 0.80→1.00; kills the
  dust and the count-dense/dollar-light false alarms.
- **Tolerant debounce** (ADR-008): FAR v2 0.51→0.38, v3 0.19→0.16, *and* better
  lead (Dec 2021 +4→+10) — the sliding window stops a flickering run-up from
  both firing late and re-triggering on every dip.
- Remaining false alarms are overwhelmingly genuine 2022 stress (Terra/3AC/FTX)
  that falls just outside the strict pre-registered D-A labels — not noise.

## Cross-protocol transfer
The same n(t) peaks at 0.999–1.000 during Compound v2 (0.9993) and Maker
(0.9997) cascades on the liquidation stream alone (count-only, no USD operating
point) — the signal transfers across protocol designs.

*Method: `METHOD.md`. Decisions: `../../docs/decisions/ADR-006..008`.*
