# CascadeSignal — project context

Early-warning system for liquidation cascades in DeFi lending. A self-exciting
(Hawkes) point process turns a protocol's live liquidation stream into a single
branching-ratio alarm `n(t) ∈ [0, 1)` that rises before a cascade. Built and
validated on Aave v2 / v3 (Ethereum), with transfer to Compound v2 and Maker.

## Tech stack

- **Python 3.12**, managed with **uv** (`make setup` / `make check`).
- Core: **numpy · scipy · pandas · pyarrow · scikit-learn** (the Hawkes model,
  labeling, evaluation).
- Live monitor: **FastAPI + uvicorn + websockets**, static web UI in
  `src/cascadesignal/live/static/`.
- Graphs/plots: **networkx · matplotlib**.
- Data ingestion: **dune-client** (Dune), Etherscan `getLogs`, public JSON-RPC,
  DefiLlama, The Graph. Data lake in `data/` via **Git LFS**.
- Quality: **pytest · ruff · black · mypy · pre-commit**.
- Paper: **LaTeX** in `paper/`.

## Key facts

- The signal is `models.hawkes.make_operating_model()` — a USD-marked blend
  (`count + 0.5·usd`), fit once and scored incrementally. `excite_col=None` is
  the count-only ablation, kept for every comparison.
- Alarm uses a tolerant debounce: fire when `k` of the last `W` bars cross the
  threshold (deployed **k=10, W=100**).
- Operating points: Aave v2 `0.99900`, Aave v3 `0.99980`. Resolved from
  `.env` (`LIVE_<P>_THRESHOLD` / `_DEBOUNCE_K` / `_DEBOUNCE_W`) → `thresholds.json`.
- Ground-truth episodes are the frozen, pre-registered ADR-001 D-A labels
  (`scripts/run_cascade_labeler.py`) — never re-fit them.

## Working principles

1. **Think before coding.** State assumptions; if multiple interpretations
   exist, surface them rather than picking silently; if a simpler approach
   exists, say so. When something is unclear, ask.
2. **Simplicity first.** Minimum code that solves the problem — no speculative
   features, abstractions, or error handling for impossible cases.
3. **Surgical changes.** Touch only what the request needs; match existing
   style; don't refactor what isn't broken. Clean up orphans your change
   creates, but leave pre-existing dead code (mention it instead).

## Discipline

The ADR-001 labels are frozen ground truth and must never be re-fit against the
model. The count-only Hawkes stays as the ablation in every comparison.
Roadmap (not deployed): extend the contagion-graph fragility covariate beyond
Aave v2.
