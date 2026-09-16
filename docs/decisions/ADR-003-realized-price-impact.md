# ADR-003: Realized price-impact component for the full severity measure

- **Status:** Accepted
- **Date:** 2026-07-15
- **Ticket:** CAS-29 (follow-on — closing the `price_impact_usd` gap ADR-002 deferred)
- **Depends on:** ADR-002 (severity formula, bad debt), CAS-16 (DEX depth/liquidity
  ingestion via The Graph, Done)
- **Blocks:** E7 (definition-sensitivity), E9 (severity regression) — any figure
  using `severity_usd_total` from before this ADR must be re-run.
- **Supersedes:** the `price_impact_usd = null` placeholder in ADR-002 and
  `labels/severity.py`'s prior module docstring.

## Context

ADR-002 fixed `severity_usd_total = total_liquidated_usd + bad_debt_usd` and left
`price_impact_usd` as an explicit null column, because CAS-16 (DEX depth
ingestion) was still Backlog and no liquidity data existed to honestly estimate
realized slippage. CAS-16 has since shipped: real daily pool-depth bars for
Uniswap v2/v3 and Curve (`data/raw/thegraph/`), a collateral→pool mapping
(`ingest/collateral_pools.py`), and slippage-curve math
(`features/slippage.py`) validated against the pulled data. This ADR fixes the
exact methodology for turning that data into a per-episode `price_impact_usd`,
per this project's pre-registration discipline (PLAN §9) — the formula must be
committed before the labeling code runs, not tuned after seeing results.

## Decision

For each episode and each distinct `collateral_asset` appearing in its
liquidations (`start_block` to `end_block` inclusive):

### 1. Sold volume

Sum `collateral_seized_usd` across all liquidation events for that asset inside
the episode. **Modeling assumption:** the cascade's entire seized collateral for
that asset is treated as landing in one venue over the episode window. Real
liquidators may split flow across venues/aggregators or spread it over time;
this assumption skews the estimate toward an upper bound, not a hidden
approximation — flagged via the extreme-shock indicator below, same treatment
CAS-16 gives its own v3/Curve simplifications.

### 2. Venue selection

Candidate pools come from `collateral_pools.COLLATERAL_POOLS[symbol]` (up to
Uniswap v2, Uniswap v3, Curve). For each candidate, look up the nearest daily
bar to the episode's `end_time`, within a **±3-day tolerance** (data is daily;
3 days absorbs an ordinary gap without admitting a stale multi-week snapshot).
Depth is `reserve_usd` (v2) or `tvl_usd` (v3/Curve). Among candidates with a bar
inside tolerance, use the **deepest same-day pool**. In practice this is a
straight v2-vs-v3 choice for every asset except stETH (Curve-only) — no asset
maps to three simultaneous candidates.

If the asset has no `COLLATERAL_POOLS` entry, or no candidate has a bar within
tolerance, that asset's contribution is **excluded** from `price_impact_usd`
(not fabricated) and its seized USD is counted in `price_impact_uncovered_usd`
instead, mirroring `compute_bad_debt`'s accounts-touched/excluded transparency.

### 3. Slippage

Apply the exact CAS-16 formula for the chosen venue, at the **realized**
shock fraction (`sold_usd / pool_depth_usd`), not the fixed `SHOCK_SIZES` grid
— same functions, arbitrary fraction instead of a grid point:

- **Uniswap v2:** `constant_product_slippage(reserve_in, reserve_out,
  shock_fraction)`. Token order (which reserve is the collateral leg) is
  derived generically, not hardcoded per asset: token0/token1 follow Uniswap's
  address-ascending convention, so `reserve_in` is whichever of
  `reserve0`/`reserve1` corresponds to the collateral asset's address versus
  the pool's other token (WETH for every asset except stETH's Curve pool;
  USDC for WETH's own entry, per `collateral_pools.py`'s documented reverse
  direction). This also makes WETH's "sell into the USDC/WETH pool in
  reverse" case fall out of the same address-comparison rule instead of being
  special-cased.
- **Uniswap v3:** `uniswap_v3_slippage(liquidity, price_quote_per_base,
  shock_fraction, fee)`, with `price_quote_per_base` selected from the
  subgraph's `token0_price`/`token1_price` by the same token-order rule
  (verified empirically against real pulled data: `token0_price` on the
  USDC/WETH 0.05% pool reads ~3496 USDC/WETH in May 2021 — the ETH price at
  the time — confirming `token0_price` = price of token1 in token0 units).
- **Curve:** `curve_slippage(balances, DEFAULT_CURVE_A, i, j, shock_fraction)`.
  The only Curve pool in scope is the stETH/ETH pool
  (`0xdc24...67022`); its `inputTokenBalances` coin order is `[0]=ETH,
  [1]=stETH` (Curve's well-documented steCRV coin ordering, confirmed against
  the real pulled balances: coin0 ≈ 5,816 ETH / coin1 ≈ 5,602 stETH in Jan
  2021, TVL consistent with ETH's price then) — hardcoded as a single index
  pair, not a general Curve integration, consistent with `collateral_pools.py`
  scoping Curve to stETH only.

`price_impact_usd` contribution for that asset = `slippage_fraction *
sold_usd`. Episode `price_impact_usd` = sum across covered assets.

### 4. Extreme-shock flag

If any asset's realized `shock_fraction` exceeds `max(SHOCK_SIZES)` (0.10,
CAS-16's own largest pre-registered shock-grid point), set
`price_impact_extreme_shock = True` for the episode. The slippage formulas
stay numerically well-defined arbitrarily far beyond this point, but treating
a multi-day cascade's entire collateral dump as landing in one pool within a
single day is increasingly unrealistic past the range CAS-16's own grid
characterizes — flagged, not capped or excluded, so downstream analysis can
decide whether to trust it (same "flag, don't fabricate or silently exclude"
precedent as ADR-001's FTX non-detection and ADR-002's bad-debt exclusions).

### Updated severity formula

```
severity_usd_total = total_liquidated_usd + bad_debt_usd + price_impact_usd
```

`price_impact_usd` is now a real number (0.0 if no assets were covered, not
null). Coverage is reported via `price_impact_covered_usd` /
`price_impact_uncovered_usd` so a 0.0 from "no coverage" is distinguishable
from a 0.0 from genuinely negligible slippage.

## Findings (recorded post-implementation)

Running this against the real Aave v2 labeled episodes (D-A: 98, D-B: 372,
D-C: 102 -- 572 total, `scripts/run_alt_labelers.py`):

- **Coverage is high:** 96.8% (D-A), 95.7% (D-B), 96.8% (D-C) of seized
  collateral USD resolved to a same-day venue within the ±3-day tolerance;
  the remainder (`price_impact_uncovered_usd`, ~$542M total across all three
  definitions) is collateral in assets/dates the ingested pool data doesn't
  cover, excluded rather than estimated.
- **Magnitude is a plausible, modest fraction of total liquidated USD:**
  median `price_impact_usd / total_liquidated_usd` is 9.8% (D-A), 3.9%
  (D-B), 10.1% (D-C); the maximum ratio across every episode is 21.1% and
  `price_impact_usd` never exceeds `total_liquidated_usd` in any of the 572
  episodes -- no blow-ups from the extreme-shock case.
- **The extreme-shock flag fires often, honestly reflecting real pool
  depth relative to cascade size:** 73% of D-A episodes, 43% of D-B, and
  74% of D-C have at least one asset whose realized shock exceeds CAS-16's
  own 10% shock-grid ceiling -- i.e. most labeled cascades involve seizing
  collateral worth more than 10% of that asset's deepest same-day pool.
  This is a real property of the golden episodes' scale (multi-$10M-$100M
  liquidation volumes against pools that were often single-digit millions
  to low tens of millions deep in 2021-2022), not a modeling artifact --
  consistent with cascades being defined, in part, by their outsized market
  impact. Per this project's "flag, don't retroactively loosen" discipline
  (ADR-001's FTX precedent), this is recorded as a scope caveat on
  `price_impact_usd`/`severity_usd_total` for any large episode, not grounds
  to change the venue-selection or tolerance rules above.
- Total `price_impact_usd` summed across all 572 episode-rows is
  ~$1.75B, against ~$538M total `bad_debt_usd` -- price impact is the
  larger of severity's two previously-missing components.

## Consequences

- `severity.py`'s prior null placeholder and its module docstring are
  superseded; any RQ1/RQ2/E9 result computed from the old
  `severity_usd_total` must be re-run against the new formula.
- E7 and E9 can now use the full three-component severity measure exactly as
  PLAN §5 specifies, across D-A/D-B/D-C.
- Any future change to venue-selection priority, the ±3-day tolerance, the
  extreme-shock threshold, or the Curve coin-index hardcoding requires a
  superseding ADR, not a silent code edit.
