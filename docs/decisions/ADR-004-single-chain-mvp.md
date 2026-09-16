# ADR-004: Single-chain MVP scope — Aave v2 / Ethereum mainnet through the first RQ2 read

- **Status:** Accepted
- **Date:** 2026-07-19
- **Ticket:** MVP-9 (P0, Decision track — MVP Issues board)
- **Depends on:** CAS-34 (Aave v3 core-event ingest — Blocked: arXiv 2512.11363
  Zenodo record still unpublished, Dune hits an execute restriction)
- **Blocks:** none directly — the critical path (CAS-28 T2 gate → CAS-31 snapshot
  materialization → CAS-8 GNN → CAS-15/E4 RQ2 read) already runs Aave-v2-only in
  practice; this ADR is the pre-registered record of that scope, not a new
  dependency on it. Any future re-expansion to multi-protocol/multi-chain
  requires a superseding ADR, not a silent `PLAN.md` edit.
- **Note on ADR numbering:** the MVP-9 ticket's acceptance criteria (Notion MVP
  Issues board) name this file "ADR-003" — that text was written before
  `ADR-003-realized-price-impact.md` (2026-07-15) claimed that number. This
  decision is filed as **ADR-004**; the ticket's filename reference is stale,
  not the deliverable itself. Same class of drift ADR-002 already documents for
  CAS ticket numbers.
- **Mirror:** Notion Decisions Log, [Mini Project II](https://app.notion.com/p/d6588eed275f82fe9898019c7eb376c1)

## Context

`PLAN.md` §3 committed the project's core scope as Aave v2+v3, Compound v2+v3,
and Maker on Ethereum mainnet, with "MVP = Aave v2+v3 (best data, biggest
episodes)". That MVP definition is unachievable as written: Aave v3 core-event
ingest is blocked (CAS-34 — the arXiv 2512.11363 Zenodo record is still
unpublished and Dune's execute restriction blocks the fallback path), and no
other protocol has core-event coverage either. Continuing to plan against a
two-protocol MVP that only one protocol can actually deliver risks quietly
drifting the working scope without a documented decision — exactly what this
project's pre-registration discipline (PLAN §9, ADR-001/002/003) exists to
prevent for cascade definitions, and the same discipline applies to project
scope itself.

Data reality forces a narrower MVP. Core-event coverage (Deposit / Borrow /
Repay / Withdraw / LiquidationCall — required for state reconstruction and the
contagion graph, not just liquidation labels) by protocol as of this decision:

| Protocol | On disk | State + graph usable? |
|---|---|---|
| **Aave v2** | **Full core events** (167 files, full 2021-01→2026-02 window) | Yes |
| Aave v3 | liquidations only — core events blocked (CAS-34) | No |
| Compound v2 | liquidations only | No |
| Compound v3 | absorb events only | No |
| Maker | liquidations only | No |

Aave v2 is the only protocol where state reconstruction and a contagion graph
are possible at all today. Every other protocol's on-disk data is a thin,
liquidations-only slice.

## Decision

**Scope the project to Aave v2 / Ethereum mainnet, 2021-01 → 2026-02, through
the first RQ2 read (E4).** This supersedes `PLAN.md` §3's "MVP = Aave v2+v3"
line for the working scope; `PLAN.md` itself is not edited by this ADR (per
this project's pre-registration discipline, superseding a committed scope goes
through an ADR, not a silent rewrite of the original text — same treatment
ADR-003 gave ADR-002's null-placeholder text).

### Why single-chain doesn't weaken RQ2

RQ2 asks whether modeling the contagion network beats aggregating independent
position risks. The contagion channels this project models — shared collateral
assets (WETH/WBTC/stETH/…), competing claims on the same DEX liquidation
liquidity, and positions wired to the same oracle feed — all live **entirely
inside Aave v2** for the full study window. Narrowing to single-chain drops
only **cross-protocol composability edges**: the hardest channel to source data
for, and, per PLAN's own channel ranking, not the dominant one. RQ2 is fully
testable on Aave v2 alone.

### Definition of Done (first RQ2 read)

The M2 temporal GNN on the Aave v2 contagion graph beats the B4
position-aggregated comparator (and B3 / Hawkes) at forecasting a cascade `h`
blocks ahead, measured by AUPRC gain with a block-bootstrap 95% CI > 0 on ≥ 3
of the 5 held-out golden episodes. A clean negative read is also a valid DoD —
it triggers the `PLAN.md` pivot to the measurement/benchmark contribution
(RQ2's own pre-registered fallback, unchanged by this ADR).

### Explicit deferral list (deferred, not deleted)

Held until after the first RQ2 read:

- Aave v3 core-event ingest (re-probe blocked on CAS-34)
- Compound v2 / v3 core-event ingest
- Maker core-event ingest
- Cross-protocol composability edges (the contagion channel this scoping
  narrows out)
- CeFi/Coinglass & ZeroMEV exogenous covariates
- Tier-2 model bake-off
- Multi-chain expansion (L2s, other chains)

None of this work is cancelled. `src/cascadesignal/labels/cascade_labeler.py`
and the severity/labeling pipeline are already protocol-parameterized
(ADR-001's scope note), so re-expansion is additive engineering once CAS-34
unblocks or another protocol gains core-event coverage — it does not require
redoing the Aave-v2-only work product.

### What's already landed under this scope (unaffected by this ADR)

State reconstruction, ADR-001's cascade definition, the cascade atlas (E1: 98
episodes, 3 golden — China'21, Dec'21, Terra'22), feature bars v0, Tier-0
baselines (E2) + Hawkes (E3), the walk-forward eval harness, and the contagion
graph snapshot builder (CAS-31) were all already built against Aave v2 data.
This ADR does not change any of that output — it formalizes the scope those
choices were already operating inside.

## Consequences

- `PLAN.md` §3's "MVP = Aave v2+v3" line is superseded for working-scope
  purposes; `PLAN.md` is not rewritten here, but any reader relying on it for
  current MVP scope must defer to this ADR and CLAUDE.md's working-focus
  section instead.
- The critical path (CAS-28 T2 gate → CAS-31 snapshot materialization → CAS-8
  GNN → CAS-15/E4) proceeds Aave-v2-only; no step in that path is blocked by
  or waiting on multi-protocol data.
- The 5 golden cascade episodes remain the held-out set, but only 3
  (China'21, Dec'21, Terra'22) are realized in Aave v2 and anchor E4's
  evaluation; FTX'22, Oct'25, and Feb'26 stay reconstructable/labelable
  (ADR-001) without being expected to clear D-A on Aave-v2-only data.
- Re-expansion to Aave v3 / Compound / Maker / multi-chain after E4 requires a
  superseding ADR, not a silent scope creep back into `PLAN.md`'s original
  two-protocol framing.
