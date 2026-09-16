# CascadeSignal — Method

How the deployed system works end to end: what the data is, how it flows through
each stage, the Hawkes branching-ratio signal and its USD weighting, how the
operating point was tuned by parameter sweep, and how it is evaluated. This
describes the system **as deployed** (USD-marked blend, ADR-007).

---

## 1. Goal

Build an **early-warning system for DeFi liquidation cascades**: a single signal,
computed from the live liquidation stream, that rises *before* a cascade and is
quiet the rest of the time. It must be **early** (fire before the damage),
**rare** (few false alarms), and **general** (run live, transfer across
protocols).

---

## 2. Data flow (pipeline)

```
 on-chain liquidation events                      per-protocol core events
 (Dune / Etherscan getLogs)                       (deposit/borrow/repay/…)
          │                                                │  [Aave v2 only]
          ▼                                                ▼
 data/raw/<protocol>/  ──►  cascade_labeler (ADR-001)   state engine (health
   liquidation stream        D-A episodes.parquet        factors) ─► graph
          │                     │  (ground truth)         fragility covariate
          │                     │                         [roadmap, not deployed]
          ▼                     │
 build_liquidation_bars         │
   1-block bars, columns:       │
   n_liquidations, liquidated_usd
          │                     │
          ▼                     │
 HawkesUnivariateBranchingRatio │
   (USD-marked blend)  ──► n(t) │
          │                     │
          ▼                     ▼
 debounce (k) ─► alarm ─────► evaluation: FAR, recall, lead
   (threshold + k)             (classify_fires vs episodes)
```

Everything downstream of the raw liquidation stream is protocol-generic. The
right-hand branch (core events → state → fragility covariate) is **Aave-v2-only
and not in the deployed signal** — it is future work.

### 2.1 Raw liquidation stream
Per protocol, the liquidation events (`LiquidationCall` on Aave, etc.) with
`block_number`, `block_timestamp`, `user`, `collateral_asset`, `debt_asset`,
`amount_usd`. Data runs to 2026-02-28 for Aave v2 (49,331 events) and v3
(23,955 events).

### 2.2 Cascade labels — the ground truth (ADR-001, "D-A")
A rolling `w`-block window anchored at a liquidation is a **cascade** iff all
three hold: **severity** (window USD ≥ the `θ`-percentile of all windows),
**breadth** (≥ `k` distinct positions over ≥ 2 accounts), and **contagion**
(≥ 2 "generations" — a burst, a >20-block gap, then a second burst on the same
collateral asset). Swept over a grid `w∈{50,100,300} × k∈{5,10,20} ×
θ∈{p99,p99.5,p99.9}`; primary point `(100,10,p99.5)`. Overlapping windows merge
into episodes. Result: **98 v2 episodes** (3 primary) and **45 v3 episodes**
(1 primary). This is fixed *before* any modelling (pre-registration) so the
definition can't be tuned to flatter the model.

### 2.3 Feature bars
`build_liquidation_bars` bins the stream into **1-block bars** (`bar_blocks=1`;
~12.5 s each). Each bar carries `n_liquidations` (event count) and
`liquidated_usd` (Σ `amount_usd`). One block per bar because n(t) only updates
when a bar closes — a wider bar throws away lead time.

---

## 3. The signal — Hawkes branching ratio n(t)

A **self-exciting (Hawkes) process** on the liquidation stream: each liquidation
raises the instantaneous rate of further liquidations, which decays over time.
The score is the **branching ratio** n(t) ∈ [0, 1) — the *self-excited share* of
the intensity, i.e. expected offspring per event. n(t) → 1 means the burst is
becoming self-feeding (near-critical); it is the early-warning indicator.

Exponential-kernel recursion on the bars (decay = `exp(-β)`):

```
R_0  = 0
R_i  = decay · (R_{i-1} + mark_{i-1})     # self-excited history, strictly before bar i
λ_i  = μ + α · R_i                         # conditional intensity (rate of events)
n(t) = α·R_i / λ_i   ∈ [0, 1)              # branching ratio = self-excited share
```

`(μ, α, β)` are fit by **Poisson maximum likelihood** on the bar counts.
`R_i` uses only marks strictly before bar `i`, so the score never peeks at the
current bar (no look-ahead). Stationarity check: effective branching ratio
`α·E[mark]/β ≪ 1` (≈ 0.001–0.002 here).

### 3.1 USD weighting — the "marked" blend (ADR-007)

Plain n(t) is driven by **event counts** — it fires on *any* clustering of
liquidations, including flurries of tiny ones (dust, and mid-size
count-dense/dollar-light bursts). But a cascade is defined by **dollar volume**.
So the self-exciting history is driven by a **blended mark**:

```
mark_i = count_i + w · (usd_i / s)        # w = 0.5  (dollar_weight)
                                          # s = mean positive per-bar USD (fit-time)
```

while the Poisson likelihood stays on the **counts** (the standard *marked*
ground-intensity form: marks modulate the future event rate, the observed
process is still event occurrence). Behaviour of `w`:

| `w` | meaning | effect |
|---|---|---|
| 0 | pure count model | fires on any clustering (the old baseline) |
| **0.5** | **deployed blend** | keeps count sensitivity for fast-cascade lead *and* the dollar volume that suppresses count-dense/dollar-light false alarms |
| → ∞ | pure USD mark | kills false alarms but loses fast-cascade lead (Jun 2022's early warning is itself dollar-light) |

`w = 0.5` is the swept optimum (§5). USD is scaled by its fit-time positive mean
`s` purely to condition the optimiser — n(t) is invariant to that scale; `s` is
persisted so a reload scores identically. `make_operating_model()` builds this
one model, used everywhere (live monitor, calibration, figures) so the deployed
signal is defined in exactly one place.

### 3.2 Fitting & persistence
- The live monitor fits `(μ, α, β, s)` once on history and **persists** them
  (`LiveModelState`); a restart resumes instead of refitting.
- For figures / historical replay, the persisted fit is loaded and the whole
  history is re-scored from the start (`R` reset to 0) — identical to what the
  monitor produced, so the figures describe the *actual* deployed signal.
- Calibration uses a **walk-forward out-of-fold** fit (expanding window, monthly
  folds) — the model never sees future data when scoring a fold.

---

## 4. The alarm — threshold + debounce

n(t) is turned into discrete alerts by two knobs:

- **threshold** — n(t) must cross this to be "in alarm."
- **tolerant debounce `k` within window `W`** (ADR-008) — fire **one** alert once
  **`k` of the last `W` bars** are above threshold (a persistence filter). `k=1`
  is fire-on-first-crossing; `W = k` is strict-consecutive; `W > k` tolerates
  dips. Near-critical run-ups *flicker* around the threshold, and strict
  consecutiveness resets on every dip — firing late *and* spawning a fresh false
  alarm on each dip-recovery. The window absorbs the dips, so it fires earlier
  **and** lowers FAR. Deployed `k = 10`, `W = 100`.

The operating point resolves as: `LIVE_<P>_THRESHOLD` / `LIVE_<P>_DEBOUNCE_K` /
`LIVE_<P>_DEBOUNCE_W` env override → else `thresholds.json` / defaults. Deployed:

| chain | threshold | k | W |
|---|---|---|---|
| Aave v2 | 0.99900 | 10 | 100 |
| Aave v3 | 0.99980 | 10 | 100 |

---

## 5. Parameter sweep — how the operating point was tuned

Three knobs interact: **threshold**, **debounce k**, and **dollar-weight w**.
They were tuned jointly against a false-alarm budget, holding the pre-registered
labels fixed.

1. **Threshold — the master lead/FAR dial.** Calibrated by walk-forward OOF to
   the max-recall point under a **≤ 1 false-alarm/week budget**
   (`recall_at_far_budget`). Recall saturates at 100% across a wide band, so
   within that band the threshold is a *lead vs FAR* choice: lower threshold =
   earlier fire on fast cascades = more false alarms. v2 was placed at 0.99900
   (FAR 0.51, all majors pre-onset) rather than the lowest calibrated value
   0.99868 (FAR 0.70) — the extra lead there was the alarm firing hours early on
   adjacent multi-day turbulence, so we took the clean leads at lower FAR.
2. **Dollar-weight w — recall & false-alarm efficiency.** A joint sweep over
   `w ∈ {0, 0.5, 1, 2, ∞} × threshold × k` showed `w = 0.5` dominates: at matched
   FAR it beats the pure-count model on *both* recall (0.86→1.00 on v2) and lead,
   and beats pure-USD on fast-cascade lead. Higher `w` over-suppresses; `w=0` is
   the count baseline.
3. **Debounce (k within window W) — FAR + lead together.** The *tolerant* filter
   (ADR-008) fires once `k` of the last `W` bars are above threshold. Moving from
   strict-consecutive (`W=k`) to `k=10 / W=100` **lowered FAR on both chains**
   (v2 0.51→0.38, v3 0.19→0.16) *and* improved lead (Dec 2021 +4→+10), because it
   stops a flickering run-up from both firing late and re-triggering on every dip.

**Why not a single magic number:** there is a real **FAR ↔ lead frontier** —
warning the *fast* cascades before onset requires a lower threshold and costs
false alarms. The deployed point sits at the knee (all majors pre-onset on v2,
FAR under budget). Fast cascades (all of v3, v2 Dec/Jun) are near their onset by
construction — the liquidation stream has no earlier signal to fire on, which is
why lead there is small and why richer covariates are the roadmap fix.

**v3 threshold note.** v3's raw calibrated threshold reports a **+192 min
"lead"** that is an *artifact* — an isolated 20-bar n(t) spike ~194 min before
onset that returns to baseline for ~900 bars before the real cascade (verified;
documented in ADR-006). v3 is therefore held at 0.9998, which cuts the spike and
reports honest onset detection.

---

## 6. Evaluation

### 6.1 Detection rule
A debounced fire is a **true warning** of an episode if it lands in that
episode's window `[start − LEAD_WINDOW, end]` (`LEAD_WINDOW = 1200` blocks ≈
4.2 h — the credit window). A fire in **no** episode's window is a **false
alarm**. Classified against **all** labelled episodes (primary + secondary).

### 6.2 Metrics
- **Recall** — fraction of episodes with ≥ 1 true warning (reported *all* and
  *major/primary*).
- **False-alarm rate** — false alarms per week, reported **full-history** and
  **out-of-sample** (years with no labelled cascade — the operating point never
  saw them; tests non-overfitting).
- **Lead (two co-reported metrics, ADR-006):**
  - **advance warning** = `start − t_alarm` (min before the cascade begins);
  - **alarm → X% of USD** = time until cumulative liquidated USD in the episode
    reaches X% (X = 10%, 50%) — the *economic reach*.
- **Protectable USD** — share of total liquidated USD that occurs *after* the
  alarm (an upper bound assuming instant intervention).

### 6.3 Discipline
Walk-forward OOF, no label leakage; the pre-registered ADR-001 labels are frozen;
the count model is kept as the baseline/ablation; any operating-point or
definition change requires a superseding ADR.

---

## 7. Deployed results (both metrics, at the operating point)

| chain | thr | FAR (full) | FAR (OOS) | recall all | recall major | protectable |
|---|---|---|---|---|---|---|
| Aave v2 | 0.99900 | 0.38/wk | 0.14/wk | 98/98 | 3/3 | 81% |
| Aave v3 | 0.99980 | 0.16/wk | 0.18/wk | 45/45 | 1/1 | 22% |

Per major cascade (advance warning · to-½-cost): **May 2021 +65 / 86**,
**Jun 2022 +7 / 26**, **Dec 2021 +10 / 23** min (Aave v2); **Feb 2025 onset / 18**
min (Aave v3). Across all 143 episodes: median advance ~+6 min, median alarm →
10% of USD ~+9 min, → 50% ~+19 min.

**vs the count-Hawkes baseline:** USD-marking lifts recall (v2 0.86→1.00,
v3 0.80→1.00) and kills the two false-alarm modes (dust storms + count-dense/
dollar-light bursts); the false alarms that remain are overwhelmingly genuine
2022 stress (Terra/3AC/FTX) that falls just outside the strict D-A labels.

**Cross-protocol transfer:** the same n(t) peaks at 0.999–1.000 during Compound
v2 and Maker cascades on the liquidation stream alone (count-only; no USD
operating point), showing the signal transfers across protocol designs.
