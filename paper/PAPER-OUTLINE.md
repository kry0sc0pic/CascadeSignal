# CascadeSignal — Full Research Paper Flow (Q1 Journal Grade)

> This is the **writing plan / narrative blueprint** for the paper. The LaTeX
> skeleton in `paper/sections/` mirrors this document one-to-one. Source of
> truth for *content decisions* is here + the technical plan
> (`.context/plans/cascadesignal-detailed-technical-research-plan-v2.md`) and the
> pre-registered ADRs in `docs/decisions/`. Keep this file and the skeleton in sync.

---

## 0. Working title & one-line pitch

**Working title:** *CascadeSignal: Forecasting System-Level Liquidation Cascades in
Decentralized Lending with Contagion-Network Point Processes.*

**Alt titles** (pick after results land):
- *Does the Network Matter? Forecasting DeFi Liquidation Cascades Beyond Aggregate Risk.*
- *Early Warning for On-Chain Liquidation Cascades: A Contagion-Graph Approach.*

**One-line pitch:** We build the largest labeled dataset of DeFi liquidation
cascades (2021–Feb 2026, five systemic episodes) and show whether modeling the
**contagion network** forecasts system-level cascades earlier and more precisely
than aggregating independent position risks — quantified as USD protectable at a
given lead time.

---

## 1. Target venue & framing

The paper has a **built-in fork** (see technical plan §4 "Gates"): the result of
RQ2 decides the framing. Write the skeleton so either framing is a short edit
away, not a rewrite.

| Framing | Trigger | Primary venue (Q1) | Alternates |
|---|---|---|---|
| **A — Methodological/economic** "network contagion improves cascade forecasting" | RQ2 network signal is positive & robust | **Journal of Financial Stability** (Elsevier) | Quantitative Finance; Journal of Banking & Finance; Journal of Empirical Finance |
| **B — Measurement/benchmark** "a rigorously labeled DeFi cascade benchmark + honest negative/null on network gains" | RQ2 null or fragile | **IEEE TKDE** / **ACM TIST** (benchmark + methodology) | NeurIPS Datasets & Benchmarks (track, not journal); Quantitative Finance (measurement) |

**Default target:** *Journal of Financial Stability* (systemic-risk framing carries
the economic contribution and the policy/protocol-design implications). Keep an
ML-forward cut ready for TKDE/TIST.

**Elsevier requirements to satisfy from day one** (both finance targets are
Elsevier): *Highlights* (3–5 bullets, ≤85 chars each), *Graphical Abstract*,
structured *Declarations* (data availability, competing interests, CRediT author
contributions), and *JEL / keyword* metadata. These are stubbed in `main.tex`.

---

## 2. Contributions (the spine — every section must serve one)

1. **C1 — Data & benchmark.** Largest labeled cascade dataset for DeFi lending
   (Aave v2/v3, Compound v2/v3, Maker, +Morpho/Euler expansion; Ethereum L1
   core, L2 stretch; Jan 2021 → Feb 2026), with reconstructed health factors,
   a **pre-registered** cascade definition, and integrity tests reconciled to
   protocol-published statistics. Released as an open benchmark.
2. **C2 — Forecasting framework.** A contagion-network formulation coupling
   **multiplex-network Hawkes** point processes with **temporal GNNs** to
   produce calibrated `P(cascade within h blocks)` + severity, with a
   branching-ratio criticality indicator.
3. **C3 — The RQ2 result.** A like-for-like test of whether the contagion
   network beats aggregating position-level risks, with block-bootstrap CIs and
   episode hold-outs — reported honestly either way.
4. **C4 — Lead-time & economics.** The lead-time-vs-precision frontier and a
   headline **USD-protectable-at-lead-time** economic metric.
5. **C5 — Robustness & causality.** Definition-sensitivity, channel/mempool
   ablations, and (optional RQ3) natural experiments (Chainlink SVR adoption,
   post-Terra parameter tightening) via diff-in-diff + agent-based counterfactual
   replay.

**Research questions:** RQ1 lead-time/precision frontier · RQ2 network vs
aggregate (make-or-break) · RQ3 do protocol design changes causally reduce
severity (optional).

---

## 3. Narrative arc (the story in five beats)

1. **Stakes.** DeFi lending intermediates tens of billions; liquidation cascades
   are the sector's systemic-risk analogue and they recur (five episodes, two
   with full mempool coverage). We want to *forecast* them, not just describe.
2. **Tension.** Cascades are contagion phenomena — but does that structure carry
   *predictive* information beyond aggregate stress? A skeptic says "this is just
   volatility/price prediction." RQ2 is the referee.
3. **Approach.** Reconstruct state, pre-register a cascade definition, build a
   channel-tagged contagion graph, and pit a network model against a strong
   aggregate-of-positions comparator under strict walk-forward evaluation.
4. **Payoff.** The frontier (how early, how precisely) + the economic figure
   (USD protectable) + the honest RQ2 verdict + what channels/features matter.
5. **So what.** Implications for protocol design (risk params, oracles/SVR,
   circuit breakers) and an open benchmark so the result is contestable.

---

## 4. Section-by-section flow (maps to `sections/*.tex`)

Word budgets assume a ~12–16k-word Q1 empirical paper (excl. appendices).

| # | File | Title | ~Words | Serves | Uses experiments |
|---|---|---|---|---|---|
| — | `00-highlights.tex` | Highlights + Graphical abstract | 120 | all | — |
| — | `00-abstract.tex` | Abstract | 220 | all | E1,E4,E6,E9 |
| 1 | `01-introduction.tex` | Introduction | 1400 | C1–C5, RQ1–3 | E1,E4,E9 |
| 2 | `02-background.tex` | Background & institutional setting | 1600 | C1,C2 | E1 |
| 3 | `03-related-work.tex` | Related work | 1400 | C2,C3 | — |
| 4 | `04-data.tex` | Data & state reconstruction | 1800 | C1 | E1, T1/T2 |
| 5 | `05-cascade-definition.tex` | Cascade definition & labeling (pre-registered) | 1400 | C1 | E1,E7 |
| 6 | `06-features-graph.tex` | Features & contagion graph | 1500 | C2 | E5,E8 |
| 7 | `07-methods.tex` | Forecasting models | 2000 | C2,C3 | E2,E3,E4,E10 |
| 8 | `08-experimental-design.tex` | Evaluation protocol | 1200 | C3,C4 | all E |
| 9 | `09-results.tex` | Results | 2600 | C3,C4,C5 | E1–E10 |
| 10 | `10-causal-analysis.tex` | Causal analysis (RQ3, optional) | 1000 | C5 | E11 |
| 11 | `11-discussion.tex` | Discussion & implications | 1200 | all | E4,E5,E9 |
| 12 | `12-limitations.tex` | Limitations & threats to validity | 700 | all | E7,E8 |
| 13 | `13-conclusion.tex` | Conclusion & future work | 500 | all | — |
| A | `99-appendix.tex` | Appendices A–F | — | C1–C5 | all |

**Section flow logic:** stakes+questions (1) → mechanics the reader needs (2) →
where we sit in the literature (3) → what we measure and how we know it's right
(4–5) → what we feed the models (6) → the models incl. the RQ2 contrast (7) →
how we grade fairly (8) → what happened (9) → why it happened / did design
changes cause it (10) → what it means (11) → what could be wrong (12) → close (13).

---

## 5. Figure & table plan (stable labels — do not renumber ad hoc)

**Figures** (`paper/figures/`, referenced as `\ref{fig:...}`):
- `fig:mechanism` (F1) — anatomy of a liquidation cascade / feedback loop schematic. §2.
- `fig:atlas` (F2) — cascade atlas timeline 2021–Feb 2026, five episodes annotated. §4/§9 (E1).
- `fig:graph` (F3) — contagion-graph snapshot (nodes, edge channels). §6.
- `fig:branching` (F4) — branching ratio n(t) around episodes. §9 (E3).
- `fig:rq2` (F5) — **headline**: AUPRC network vs aggregate, bootstrap CIs, per-episode. §9 (E4).
- `fig:ablation-channel` (F6) — channel ablation marginal value. §9 (E5).
- `fig:frontier` (F7) — lead-time vs precision frontier. §9 (E6).
- `fig:mempool` (F8) — mempool ablation ΔAUPRC (Aug'23–Feb'26). §9 (E8).
- `fig:economics` (F9) — USD protectable vs lead time. §9 (E9).
- `fig:calibration` (F10) — reliability diagram / calibration. §9.
- `fig:importance` (F11) — feature/channel importance (mechanism). §11.
- `fig:did` (F12) — RQ3 event study (SVR adoption). §10 (E11).
- `fig:graphical-abstract` — one-panel summary for submission.

**Tables** (`paper/tables/`, referenced as `\ref{tab:...}`):
- `tab:sources` (T1) — data source registry. §4 (from technical plan §1).
- `tab:episodes` (T2) — golden-episode stats + reconciliation vs protocol stats. §4/§9 (E1,T1-test).
- `tab:defgrid` (T3) — cascade-definition grid + label counts. §5 (E1).
- `tab:descriptive` (T4) — feature descriptive stats / stylized facts. §4.
- `tab:models` (T5) — model roster (Tier 0/1/2, families). §7.
- `tab:main` (T6) — **headline results**: models × metrics. §9 (E2,E4,E10).
- `tab:sig` (T7) — significance (Diebold-Mariano / block bootstrap). §9.
- `tab:defsens` (T8) — definition-sensitivity robustness. §9 (E7).
- `tab:ablations` (T9) — ablations summary (channel + mempool). §9 (E5,E8).
- `tab:did` (T10) — diff-in-diff estimates. §10 (E11).
- `tab:repro` (T11, appendix) — reproducibility / benchmark card. App. F.

---

## 6. Experiment → section crosswalk (from technical plan §7)

| Exp | Content | Lands in |
|---|---|---|
| E1 | Cascade atlas + per-episode dossiers | §4, §9.1; `tab:episodes`, `fig:atlas` |
| E2 | Tier-0 baselines, walk-forward | §9.2; `tab:main` |
| E3 | Hawkes fits; branching ratio n(t) | §9.3; `fig:branching` |
| E4 | **RQ2 headline**: M2 vs B4 (vs B3/M5) | §9.4; `fig:rq2`, `tab:main`, `tab:sig` |
| E5 | Channel ablation | §9.5; `fig:ablation-channel`, `tab:ablations` |
| E6 | Lead-time frontier | §9.6; `fig:frontier` |
| E7 | Definition sensitivity | §9.7; `tab:defsens` |
| E8 | Mempool ablation | §9.8; `fig:mempool`, `tab:ablations` |
| E9 | Severity + economic USD metric | §9.9; `fig:economics` |
| E10 | Tier-2 bake-off | §9.10; `tab:main` |
| E11 | RQ3 natural experiments + counterfactual replay | §10; `fig:did`, `tab:did` |

---

## 7. Reproducibility & rigor checklist (Q1 gate — App. F)

- [ ] Pre-registered cascade definition (ADR-001) timestamped **before** any model sees labels.
- [ ] Data availability statement + versioned dataset (Git LFS) + release DOI.
- [ ] Strict expanding-window walk-forward; **no** future leakage (T4 shuffled-future canary).
- [ ] Episode hold-outs reported separately from pooled metrics.
- [ ] ≥5 seeds for deep models; report mean ± CI, not single runs.
- [ ] Block bootstrap + Diebold-Mariano for headline comparisons.
- [ ] AUPRC primary (imbalanced), plus recall@fixed-FAR, Brier/calibration, economic USD.
- [ ] Compute budget + hyperparameter search disclosed (App. D).
- [ ] Code + configs released; `make e2e-sample` reproduces a slice in CI.
- [ ] Honest reporting of RQ2 sign (positive **or** null) — no HARKing.

---

## 8. Writing conventions

- One `\input{sections/NN-name.tex}` per section from `main.tex`; never write
  prose in `main.tex`.
- Each section file opens with a `% FLOW` comment block: purpose, key message,
  target length, figures/tables, experiment mapping.
- Guidance visible in draft PDFs uses `\wnote{...}` (blue) and `\todoitem{...}`
  (red); both vanish when `\draftnotesfalse` (camera-ready).
- Figures use `\figplaceholder{...}` until the real asset exists (compiles with
  no image files).
- Stable `\label` names as in §5–§6 above. Cite with `\citep`/`\citet` (natbib).
- Numbers, episode stats, and reconciliation targets come from the data pipeline
  and `docs/episodes/`, never hand-typed into prose without a source.
