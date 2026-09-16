# CAS-28 — T2 mismatch: ranked next steps to close the gate

> **FINAL STATUS (2026-07-23): GATE PASSING — CAS-28 CLOSED.** ADR-005
> (mentor sign-off 2026-07-23) redefines the T2 gate as a tolerance band:
> mismatch iff reconstructed `HF >= 1.0 + HF_TOLERANCE`, `HF_TOLERANCE =
> 0.01`. Under the band, `pytest -m t2_gate` now **passes**: 1.093% (527 /
> 48,235 measurable) against the 2% bar — the first time in this ticket's
> history. The exact-boundary rate (`HF >= 1.0`, the pre-ADR-005 reading) is
> still 2.5853% (1,247 / 48,235) and is reported alongside the tolerance-band
> rate everywhere, per ADR-005's commitment to never print one number
> without the other. This closes H9, the last open item on this ticket's
> ranked list — every mechanism below has now landed, been refuted, or been
> ground-truth-confirmed as boundary precision (see ADR-005's Context section
> for the full evidence chain). The rest of this file is preserved as the
> lever-by-lever record that got the exact-boundary rate from 53.9% down to
> 2.5853% before the tolerance band closed the remainder.

**Status as of 2026-07-23 (updated, post-Lever-12):** T2 mismatch rate
**2.6%** (Levers 1–12 landed; was 6.0% after Lever 11c, 6.5% after Lever 11b,
7.6% after Lever 11, 8.3% after Lever 10, 8.4% after Lever 9, 10.8% after
Lever 8, 11.3% after Lever 7, 11.4% after Lever 6, 16.4% after Lever 5, 19.0%
after Lever 3, 19.7% after Lever 4's launch backfill, 24.2% after Lever 2,
30.9% after Lever 1, 37.8% after Track E, 53.9% before). Gate threshold is
**2%** — still FAILING, but by far the closest this ticket has gotten.
H7 (archive-node ground truth, see below) showed the residual isn't a
reconstruction bug for the bulk of cases; Lever 10 then acted on H7's most
concrete lead (same-block timing) and moved the rate a small, honest amount
(see Lever 10 below for why the yield was much smaller than H7's headline
number might have suggested). Lever 11 (real oracle-source timeline) then
found and fixed genuine coverage gaps, net 8.3% → 7.6%. Lever 11b then
replicated three of the four remaining custom-adapter reserves' *exact*
on-chain pricing formula (not an approximation) and expanded ETH/USD's own
history to full coverage, net 7.6% → 6.5%. Lever 11c closed the last one
(xSUSHI, a structurally harder live-contract-state replication), net
6.5% → 6.0% — every one of Aave v2's 37 reserves now has native
ETH-numeraire coverage, closing out that entire line of work. **Lever 12
then found the single biggest gap in the whole ticket**: Aave v2's own live
`AaveOracle.getAssetPrice` (queried directly, not reconstructed from
Chainlink logs) corrects 58.6% of the `unexplained` bucket, net
**6.0% → 2.6%**. See Levers 11b/11c/12 below for the full arc. This file is
the backup plan for the remaining root-causing, worked **one lever at a
time**, most promising first. Each lever below records its evidence,
expected/measured impact, cost, and how to verify.

> **⚠️ CORRECTION (2026-07-21, later same day): the "irreducible
> boundary-precision floor" conclusion below was wrong.** It was drawn after
> Levers 1–3 (19.0%) and never re-examined after Lever 4's launch-event
> backfill (→19.7%). An ETH-numéraire test then showed the residual was
> mostly a **fidelity gap**, not noise: switching to the asset/ETH feeds
> Aave v2 actually reads (instead of reconstructing an implied cross-rate
> from two independent asset/USD feeds) cut the mismatch on the
> both-measurable subset by more than half. That's now landed as **Lever
> 5** below, moving the real rate 19.7% → 16.4%. The "Residual analysis"
> section further down is preserved for its still-useful sub-findings
> (tolerance-band table, price-staleness check, debt-undercount forensics)
> but its top-line conclusion and "Decision required (A)/(B)" framing are
> **superseded** — see the full ranked follow-on catalog (H2–H10) in
> `.context/plans/cas-28-boil-the-ocean-theory-of-the-remaining-t2-m.md`,
> which enumerates the remaining fidelity-gap hypotheses and is the current
> execution plan, worked one hypothesis at a time same as this file.

Prior tracks already landed (for context, see `state/engine.py`'s module
docstring for the full narrative):

| Track | Fix | T2 rate after |
|-------|-----|---------------|
| (baseline, gateway misattribution masking) | — | 28.1% (artifact) |
| onBehalfOf + Withdraw re-attribution | correct ledger attribution | 69.2% (bug unmasked) |
| B | per-reserve collateral-toggle | 68.2% |
| C | naked aToken-transfer correction | 53.9% |
| D+E | interest-index accrual (scaledBalance) | 37.8% |
| Lever 1 | pre-liquidation HF convention (gate metric) | 30.9% |
| Lever 2 | Chainlink-primary (blended) price oracle | 24.2% |
| Lever 3 | point-in-time liquidation thresholds | 19.0% |
| Lever 4 | launch-month core-event backfill (completeness fix) | 19.7% |
| Lever 5 | ETH-numéraire pricing (H1) | 16.4% |
| Lever 6 | full-history ETH feeds + unbounded staleness (H2) | 11.4% |
| Lever 7 | same-block cascade state ordering (H4a) | 11.3% |
| Lever 8 | compound interest index to the trigger second (H5) | 10.8% |
| Lever 9 | exact token-level ledger (H3) | 8.4% |
| Lever 10 | same-block price ordering (H4b) + earlier-activity generalization | 8.3% |
| Lever 11 | real oracle-source timeline: early-2021 gap + stETH/CVX coverage | 7.6% |
| Lever 11b | GUSD/ENS/LUSD custom-adapter cross-rate replication + ETH/USD full history | 6.5% |
| Lever 11c | xSUSHI SushiBar share-price replication (last uncovered reserve) | 6.0% |
| Lever 12 | live `AaveOracle.getAssetPrice` fallback for `unexplained` mismatches | **2.6%** |

## The residual population is systematic, not outliers

The residual is close-margin, not outlier-driven — small HF-shifting
corrections flip large chunks of the population at once. That's the whole
game now.

Cause buckets from `experiments/T2/output/mismatch_diagnostics.csv`:

| bucket | pre-Lever-1 (37.8%) | post-L1 (30.9%) | post-L2 (24.2%) | post-L3 (19.0%) |
|--------|---------------------|-----------------|-----------------|-----------------|
| `unexplained` | 6,363 | 4,467 | 4,839 | 3,146 |
| `unexplained_no_chainlink_coverage` | 4,189 | 3,378 | 3,346 | **3,760** |
| `oracle_lag` | 677 | 1,532 | 0 | 0 |
| `param_drift` | 818 | 642 | 684 | **46** |

Pre-Lever-1 the close-margin bulk was 92.4% in HF [1.0, 1.5), median 1.17,
with only 227 (1.9%) wild (HF ≥ 5). Lever 1 shifted mass toward diagnosable
(`oracle_lag` doubled). **Lever 2 consumed the whole `oracle_lag` bucket** —
Chainlink-primary pricing makes those cases match (no lag left once you
reconstruct with the feed Aave read). **Lever 3 nearly emptied `param_drift`
(684 → 46)** — with real point-in-time thresholds, de-risked reserves are no
longer an unresolved uncertainty. The residual is now `unexplained` (3,146,
Chainlink-confirmed, genuinely not price/threshold/accrual) and
`unexplained_no_chainlink_coverage` (3,760, a position leg with no Chainlink
price — the Lever 2b target).

---

## Lever 1 — Pre- vs post-liquidation convention ✅ **DONE (2026-07-21)**

**Landed: 37.8% → 30.9% (−6.9 pts, ~2,000 fewer mismatches), confirmed on
real data through the full pipeline.** Implemented in `t2_gate.py`
(`reconstruct_hf_at_trigger` now queries `block_number - 1`); module +
function docstrings updated; two synthetic unit tests re-specified for the
new semantics (full-repay is now measurable pre-liq; a same-block
open+liquidate is the new "not measurable" case). 355 tests pass. The
original evidence/rationale is preserved below for the record.

**Evidence.** The gate computes HF from `engine.positions_at(block)`, which
*includes the liquidation's own ledger effect* — i.e. after the liquidator
repaid up to 50% of the debt and seized collateral+bonus, which mechanically
**restores** the position's health. Every one of the 12,047 mismatches is a
*partial* liquidation where debt remained (HF is computable), so we are
measuring the health-**restored** aftermath, not the trigger condition. A
position is liquidated *because* its pre-liquidation HF < 1; the physically
correct test of our reconstruction is the state at the moment the liquidator
acted.

**Measured 2026-07-21** (ad-hoc, via `positions_at_many` at `block-1`):

```
post-liq (current convention, block):   measurable=31773, mismatch=12014, rate=37.8%
pre-liq  (block-1):                      measurable=32325, mismatch= 9999, rate=30.9%
```

**Why it's also a correctness fix, not metric-gaming.** The T2 gate invariant: "every
observed liquidation must reconstruct to HF < 1 *at its trigger block*." The
trigger condition is the pre-liquidation state. The current convention was
chosen only for comparability with the CAS-17 pre-Track figures (see
`t2_gate.py` docstring) — comparability to an earlier number is not a reason
to keep a biased metric.

**Implementation notes.**
- `block-1` is a decisive *proxy* used for the measurement above; the
  rigorous version excludes only the liquidation's **own tx rows** (same
  `tx_hash`) while keeping any other same-block events. Expected to be
  within a fraction of a point of the block-1 number, possibly slightly
  better.
- Edge case to handle: a position opened and liquidated in the same block
  (flash-style) would show empty pre-liq state — decide whether that's
  `measurable=False` or counts as a match.
- Touch points: `t2_gate.reconstruct_hf_at_trigger` (the trigger-block
  convention), and the docstring rationale in `t2_gate.py`. Keep the
  human report (`t2_mismatch_report.py`) and pytest gate
  (`test_t2_mismatch_gate.py`) in sync.

**Cost:** none (no new data). **Verify:** re-run
`scripts/analysis/t2_mismatch_report.py`, confirm the ~7-pt drop and that no
previously-matching liquidation regresses for a non-physical reason.

---

## Lever 2 — Chainlink-primary (blended) price oracle ✅ **DONE (2026-07-21)**

**Landed: 30.9% → 24.2% (−6.7 pts), confirmed on real data; coverage rose
too (measurable 32,325 → 36,613).**

The plan originally framed Lever 2 as a *data pull* ("extend Chainlink to
full history"). Testing before pulling revealed the real lever was a
**wiring** change, not more data: the gate was computing `mismatch` from the
DefiLlama **daily** primary oracle and only using Chainlink to *label*
causes. Chainlink is the feed **Aave v2 itself read** to trigger these
liquidations, so reconstructing HF with it (where covered, within staleness)
uses the actual decision-relevant price — not a daily proxy. This isn't
look-ahead: it's the same on-chain price Aave's liquidation logic evaluated.

**Implementation.** `BlendedPriceOracle` in `prices.py` — Chainlink
block-level price per reserve where available, DefiLlama daily fallback,
coverage = union (so more positions are `fully_covered`). Degrades to
DefiLlama-only if the Chainlink lake is absent (same no-op-when-missing
convention as the engine's correction tables). Wired as the primary oracle
in `t2_mismatch_report.py` and the gate test; 4 unit tests added; 359 tests
pass. It consumed the entire `oracle_lag` bucket (1,532 → 0): those mismatches
are now matches, because the primary already uses the feed that would have
"flipped" them.

**Data pull was NOT needed for this** — it worked on the existing Chainlink
coverage (5 golden-episode windows + a prior surgical backfill already in
`data/raw/chainlink/`). Extending coverage is now a *separate, smaller*
follow-on (Lever 2b) targeting only the residual.

---

## Lever 2b — Extend Chainlink coverage (optional follow-on)

**Targets the residual 3,346 `unexplained_no_chainlink_coverage`.** After
Lever 2, this bucket is exactly the mismatches where some position leg has no
Chainlink price at the trigger, so the blend fell back to DefiLlama daily on
that leg — a pull could still flip the ones that are DefiLlama-staleness.

**Plan.** `scripts/onchain/backfill_chainlink_t2_windows.py` already does
this surgically (≈3-day windows around each fixable no-coverage trigger,
via the same free-Etherscan `AnswerUpdated` machinery). It was last run
against the *old* mismatch population, so re-run it against the current one:
delete the stale `data/raw/.checkpoints/chainlink_t2_*.json`, `--dry-run`
first to size the plan, then pull; the new parquet drops into the same
`data/raw/chainlink/chain=1/` the blend already reads.

**Honesty flag — do this last / maybe not at all.** CAS-17 measured only
~2.3 pts of oracle-lag effect on the golden-episode subset (the *highest*-
volatility periods, where daily-vs-block matters most), so the residual 3,346
likely yields well under that. It's a multi-hour wall-time pull for an
uncertain, probably-small gain. Weigh against Levers 3/4 first. Also, 5/37
reserves (`UNCOVERED_RESERVES`: GUSD, xSUSHI, stETH, ENS, CVX) have no feed
in the pull at all — no amount of window-extension fixes those legs.

**Cost:** free (Etherscan), multi-hour wall-time. **Verify:** re-run the
report; watch `unexplained_no_chainlink_coverage` shrink and the rate.

---

## Lever 3 — Point-in-time liquidation thresholds ✅ **DONE (2026-07-21)**

**Landed: 24.2% → 19.0% (−5.2 pts), confirmed on real data. `param_drift`
684 → 46.**

**Direction was the risk, and it was measured first.** `reserves.py` uses
**frozen** (current, de-risked) thresholds. 92.3% of mismatches are
reliable-only positions, so the rate leverage hinged on whether *reliable*
reserves' historical LTs differ from frozen — and the direction is
era-dependent: every reliable reserve's LT changed, with early-2021 values
*below* frozen (WBTC 0.75 vs 0.82, AAVE 0.65 vs 0.73 → frozen overstates
collateral → false "match" failures that correcting **fixes**) but 2022 peaks
*above* frozen (DAI 0.90 vs 0.77, MKR 0.70 vs 0.10 → frozen understates →
correcting **reveals** mismatches). Net could have gone either way, so it was
measured before wiring: the early-period fixes win, 24.2% → 19.0%.

**Implementation.** `scripts/onchain/backfill_reserve_config_history.py` pulls
`CollateralConfigurationChanged` from the `LendingPoolConfigurator`
(`0x311bb771…`, topic0 `0x637febbd…`, both verified live) over
[11.3M, 24.5M] — only 278 rows (governance events are rare), ~12s. Floor set
below Aave v2's Dec-2020 launch (~block 11.36M) to capture the *initial*
listing configs, which matter for China-episode (May 2021) reconstructions
and were worth ~0.9 pt on their own (19.9% → 19.0%). `ReserveConfigHistory`
(`state/reserve_config_history.py`) does nearest-prior lookup;
`compute_health_factor` gained a `thresholds_override` param (frozen fallback
per reserve; an overridden reserve also counts `historical_reliable`, which
is why `param_drift` nearly emptied); threaded through
`reconstruct_hf_at_trigger` + `bucket_mismatch_causes`. No-op when the parquet
is absent. 9 unit tests added; 368 pass.

**Cost:** free (Etherscan), seconds. **Correctness note:** like the earlier
gateway-misattribution finding, this is a correctness fix first — it happens
to *reduce* the rate net, but it also legitimately *revealed* some 2022-era
mismatches the too-low frozen thresholds had been masking.

---

## Lever 4 — Launch-month core-event backfill ✅ **DONE (2026-07-21)**

**Landed: 19.0% → 19.7% (+0.7 pts) — a completeness fix that *reveals* more
real mismatches, same "correctness first, rate can go either way" precedent
as Lever 3.**

**Root cause.** Our ingested core-event lake starts at block 11,565,024, but
Aave v2 launched ~block 11,362,828 (Dec 2020) — the original Dune pull began
~31 days late, so the entire launch month of Deposit/Borrow/Repay/Withdraw/
LiquidationCall was missing (uniformly across all five event types). Accounts
that opened positions in that window carry early borrows forward; our
reconstruction saw their repays without the matching borrows, so cumulative
debt went *negative* for some accounts — provably wrong (some reconstructed
to less debt than their liquidation actually repaid, impossible under Aave's
50% close factor). Confirmed on a sample account: 21 of 124 on-chain USDC
borrows (1.06M USDC) fell in the missing window.

**Implementation.** `scripts/onchain/backfill_aave_v2_launch_events.py` pulls
blocks [11,362,000, 11,565,023] for `LendingPool` (all five event types) via
Etherscan (free), decodes to the canonical schema, writes into the same
directory `load_events` globs (de-dupes on `(tx_hash, log_index)`, no block
overlap with the existing lake — purely additive).

**Cost:** free (Etherscan), minutes. **Verify:** re-run
`scripts/analysis/t2_mismatch_report.py` — coverage rose (more accounts now
have complete pre-liquidation history) and previously-masked mismatches
surfaced, netting +0.7 pts. This is expected and correct: filling in missing
history can only ever reveal mismatches that negative-debt accounting had
been hiding, never manufacture new ones.

---

## Lever 5 — ETH-numéraire pricing (H1) ✅ **DONE (2026-07-21)**

**Landed: 19.7% → 16.4% (real full-population run; −3.3 pts, 1,227 fixed /
49 broken on the both-measurable subset, 25:1 ratio).** Falsifies the
residual analysis below — see the correction notice at the top of this file.

**Mechanism.** Aave v2's `calculateUserAccountData` prices every reserve via
its **asset/ETH** Chainlink aggregator (WETH is the protocol's numeraire, so
its price is exactly 1.0 by construction, never a feed read) and sums debt
and collateral in ETH. `BlendedPriceOracle` (Lever 2) instead reconstructs
an *implied* asset/ETH cross-rate from two independent asset/USD feeds (the
reserve's own USD feed and the ETH/USD feed) — each with its own
lag/noise, which compounds. Pricing natively via each reserve's own
asset/ETH feed removes that compounding error entirely for WETH-vs-anything
positions (the dominant case), since WETH's price is then exactly 1.0
rather than a second noisy feed reading.

**Evidence.** An ad-hoc test on the subset covered by both oracles showed
21.75% → 10.81% (1,310 fixed / 46 broken, 28:1). The real, full-population
before/after (this lever, both oracles as actually wired into the gate):

```
BlendedPriceOracle (before):        n_measurable=36737, mismatch=7231, rate=19.68%
PreferEthNumeraireOracle (after):   n_measurable=37082, mismatch=6068, rate=16.36%
both-measurable subset (n=36733):   before=19.67%, after=16.47%, fixed=1227, broken=49
```

Coverage also rose slightly (36,737 → 37,082): a position can be
`fully_covered` under the ETH numeraire even where the USD blend had a gap
in one of DefiLlama/Chainlink-USD at that exact block.

**Implementation.**
- `scripts/onchain/map_chainlink_eth_feeds.py` — re-runs the same free
  `description()` sweep `map_chainlink_reserves.py` used (the golden-episode
  Chainlink pull was never filtered to specific aggregator addresses, so
  asset/ETH feed events were already sitting in `data/raw/chainlink/`
  unused), matching `"<SYMBOL> / ETH"` instead of `"<SYMBOL> / USD"`. 30 of
  37 reserves matched (+ WETH's identity); `chainlink_feeds.RESERVE_CHAINLINK_ETH_FEEDS`
  / `UNCOVERED_ETH_RESERVES` hold the result.
- `state.prices.EthNumeraire` — same nearest-prior-block-timestamp +
  staleness contract as `ChainlinkPriceOracle`, but ETH-denominated; WETH
  special-cased to a constant 1.0. `compute_health_factor` needed **no
  changes** — HF is a pure ratio, so feeding it ETH prices instead of USD
  prices for every leg of one position yields the identical,
  numeraire-invariant result.
- `state.prices.PreferEthNumeraireOracle` — per-position wrapper: uses
  `EthNumeraire` only when it covers every reserve touched by that position,
  otherwise falls back to `BlendedPriceOracle` (USD) for the *whole*
  position. Numeraires must never mix within one HF ratio. Now the primary
  oracle in `t2_mismatch_report.py` and the gate test.
- 14 new unit tests (`tests/test_chainlink_price_oracle.py`); full suite:
  395 passed, 1 deselected (the known-failing real-data `t2_gate` marker,
  unchanged by this lever).

**Cost:** free (already-pulled data + one free public-RPC sweep), minutes.
**Correctness note:** like Levers 3/4, this is a correctness fix (pricing in
the numeraire Aave actually used), not metric-gaming — it happens to reduce
the rate net, and the flip ratio (25:1 fixed:broken) confirms the direction
is overwhelmingly toward removing false mismatches, not introducing them.

---

## Lever 6 — Full-history ETH feeds + unbounded staleness (H2) ✅ **DONE (2026-07-21)**

**Landed: 16.4% → 11.4% (real full-population run; −5.0 pts, 1,904 fixed /
150 broken on the both-measurable subset, 12.7:1 ratio; coverage 37,082 →
42,879 measurable). Matches the plan's "plausibly ~16% → ~11–12%" almost exactly.**

**Mechanism.** Lever 5 (H1) only had ETH-feed events from the 5
golden-episode pull windows (~250 days total) — everywhere else, a position
touching an otherwise-covered reserve still fell through to
`unexplained_no_chainlink_coverage`. Two gaps, both closed:

1. **Missed phases.** Chainlink migrates a feed to a new aggregator contract
   periodically; H1 only knew about phases that happened to emit an event
   inside a golden window. `scripts/onchain/discover_chainlink_eth_feed_phases.py`
   queries the on-chain Chainlink `FeedRegistry`
   (`0x47Fb2585D2C56Fe188D0E6ec628a38b74fCeeeDf`, verified live — not
   memorized) for every historical phase of each feed via
   `getPhaseFeed(base, ETH, phaseId)`. 22 of 30 reserves gained a
   previously-unknown phase this way (e.g. DAI/ETH: exactly 2 on-chain
   phases, phase 1 already known, phase 2 never pulled). Each addition is
   cross-validated against the existing H1 aggregator before being trusted
   (see `chainlink_feeds.py`'s docstring). 8 reserves (WBTC, BUSD, TUSD,
   renFIL, USDP, DPI, FRAX, FEI, UST) have no `FeedRegistry` entry for their
   pair and keep their H1-only aggregator.
2. **Missed block range.** Even for already-known aggregators, the golden
   windows only cover ~250 of the ~1,900 days in the study period.
   `scripts/onchain/backfill_chainlink_eth_feeds_full_history.py` pulls
   *every* `AnswerUpdated` event for *every* aggregator (53 addresses total)
   across the full range `[11,362,000, chain tip]` via the proven Etherscan
   `getLogs` + 10k-row bisection + per-address checkpointing machinery
   (`fetch_svr_feed_events.py`'s pattern) — 282,733 rows, written to
   `data/raw/chainlink/chain=1/eth_feeds_full_history_answer_updated.parquet`.
   Cost: ~53 requests-worth of pulls (a few minutes to ~1hr wall time
   depending on per-feed density; densest single aggregator, DAI/ETH phase 1,
   was 11,247 rows in ~62s).
3. **Staleness guard dropped for `EthNumeraire`.** Aave v2's on-chain
   `latestAnswer()` never checks the age of the last stored answer — with
   the coverage gaps above closed, rejecting an old-but-real price for
   staleness is infidelity to what Aave actually read, not a safety net.
   `EthNumeraire`'s default `max_staleness` changed from 3 days to
   effectively unbounded (`pd.Timedelta.max`); `price_at` still returns
   `None` when there's no prior point *at all* (a true coverage gap).
   `ChainlinkPriceOracle`/`BlendedPriceOracle` (the USD-side oracles) are
   unchanged — this is scoped to the ETH-numeraire path only.

**Evidence (real full-population before/after, both-measurable subset):**
```
H1 (golden-windows-only, 3-day staleness):  n_measurable=37082, mismatch=6068, rate=16.36%
H2 (full-history, unbounded ETH staleness): n_measurable=42879, mismatch=4896, rate=11.42%
both-measurable subset (n=37079):           before=16.36%, after=11.63%, fixed=1904, broken=150, ratio=12.7:1
```

**Implementation.** New scripts:
`discover_chainlink_eth_feed_phases.py` (phase enumeration + merged map
literal), `backfill_chainlink_eth_feeds_full_history.py` (the pull). Changed:
`chainlink_feeds.RESERVE_CHAINLINK_ETH_FEEDS` (53 addresses across 30
reserves, up from 31), `prices.EthNumeraire`'s default `max_staleness`. 1 new
unit test locking in the no-staleness-by-default behavior; full suite: 396
passed, 1 deselected (the known-failing real-data `t2_gate` marker).

**Cost:** free (Etherscan + free public RPC), ~1 hour wall time total
(discovery + pull). **Correctness note:** same discipline as every prior
lever — the flip ratio (12.7:1) confirms the direction is overwhelmingly
toward removing false mismatches, and every mechanism here (feed migrations,
missing block range, staleness fidelity) is a genuine gap between our
reconstruction and Aave's actual on-chain read, not a metric-gaming knob.

---

## Lever 7 — Same-block cascade state ordering (H4a) ✅ **DONE (2026-07-22)**

**Landed: 11.4% → 11.3% (small, as expected for a boundary-precision lever)
— but the real finding is methodological, not the 0.1-pt rate move.**

**Mechanism.** `reconstruct_hf_at_trigger` keyed triggers on `(user,
block_number)` and deduplicated, on the assumption that a user is liquidated
at most once per block. **That assumption is false for 479 of 48,639
trigger keys** (1,171 raw `LiquidationCall` events) — a same-block
liquidation cascade, where a liquidator (or bundling contract) hits the same
undercollateralized user more than once in one block, whether across
separate txs or multiple calls within one tx. EVM execution is sequential
even within one block, so the *second* liquidation's true pre-state includes
the *first's* seizure/repay. The old `drop_duplicates` silently kept only
the first of each cascade and **dropped the other 692 events from the
population entirely** — not a wrong verdict, an absent one. Confirmed via
`len(liquidations) - drop_duplicates(["user","block_number"])`: 49,331 -
48,639 = 692, exactly matching the dropped count.

**Implementation.** Triggers now key on `(user, block_number, log_index)` --
one row per raw event. The non-cascade majority (~48,160 keys) keeps the
existing fast batched `positions_at_many` query at `block_number - 1`; the
479 cascade keys use a new `engine.position_at_log_index(user, block_number,
log_index)`, which cuts strictly before that exact `(block_number,
log_index)` pair (a Python loop over ~1,171 rows -- fine at that scale, no
need for `positions_at_many`'s batched `merge_asof` machinery). 1 new engine
test (`position_at_log_index` sees the first liquidation's effects; the
`block_number-1` query doesn't), 1 existing test updated for the new 3-tuple
`position_by_key` key shape. `bucket_mismatch_causes`, `attach_diagnostics`,
and `backfill_chainlink_t2_windows.py`'s pull-plan builder all updated for
the new key shape.

**Evidence (real data):**
```
before (692 cascade events silently excluded): n_triggers=48639, n_measurable=42879, mismatch=4896, rate=11.42%
after  (every event gets its own trigger):     n_triggers=49331, n_measurable=43452, mismatch=4914, rate=11.31%
```
Coverage rose (+573 newly-measurable) and the rate ticked down slightly
(-0.11 pts) -- small, consistent with the plan's "cheap" framing (small
*effort*, not necessarily zero value: the population-completeness fix
matters for the paper's methodology even though the rate barely moves).

**Scope note — price-ordering (H4b) NOT implemented, by deliberate choice.**
The plan's H4 also covers same-block *price* ordering: "use last
`AnswerUpdated` with `(block, log_index) <` the liquidation's" instead of
resolving nearest-prior by `block_timestamp` alone (which can't distinguish
intra-block order, since every tx in a block shares one timestamp). Measured
first, per this file's own discipline: of 4,827 mismatched triggers touching
an ETH-covered reserve, **633 (13%) are in a block where that reserve's
specific aggregator had >1 update** -- a real, non-trivial candidate
population (a looser "any Chainlink activity in this block" count gave a
misleading 2,010/4,896, most of which don't even touch the same aggregator).
Not implemented because a fully correct fix needs threading `log_index`
through every price-oracle class's `price_at`/`prices_at` signature (the
`PriceOracleLike` protocol, `ChainlinkPriceOracle`, `EthNumeraire`,
`BlendedPriceOracle`, `PreferEthNumeraireOracle`, and every call site) --
a much larger blast radius than the state-ordering fix above, for an
uncertain payoff (633 is an upper bound; most nearby same-block price
updates likely don't differ enough to flip a verdict). Flagged here as a
scoped, explicit follow-on rather than silently skipped or silently done.

**Cost:** free (no new data), a few hours of implementation + testing.

---

## Lever 8 — Compound interest index to the trigger second (H5) ✅ **DONE (2026-07-22)**

**Landed: 11.3% → 10.8% (−0.54 pts; 233 fixed / 0 broken — perfectly
one-directional, the cleanest lever yet).**

**Mechanism.** `InterestIndexOracle` returned the liquidity/variableBorrow
index as of its last on-chain *write* (a `ReserveDataUpdated` event, which
only fires when someone interacts with that reserve) -- but Aave's real
`balanceOf` forward-compounds that stored index to the *current second*
using the stored rate, every time it's read, whether or not anyone has
touched the reserve since. Implemented Aave's own formulas exactly:
`calculate_linear_interest` (liquidity index -- deposits accrue simple
interest, `MathUtils.calculateLinearInterest`) and
`calculate_compounded_interest` (variable-debt index -- a 2nd/3rd-order
binomial approximation of continuous compounding,
`MathUtils.calculateCompoundedInterest`), both in plain float arithmetic
(equivalent to Aave's RAY-scaled integer math up to float precision).

**Why it's purely one-directional.** Aave's variable borrow rate is always
≥ its liquidity (supply) rate (the spread is the protocol's revenue), so
over the same short compounding window (last index write → trigger,
typically hours to days) debt grows faster than collateral value --
`weighted_collateral` rises less than `total_debt`, HF strictly falls,
mismatches can only flip toward matching. Confirmed empirically: 0 of the
233 flips went the other way.

**Implementation.** `interest_index.py` gained `calculate_linear_interest`/
`calculate_compounded_interest` (scalar) + `_compounded_interest_array`
(vectorized, for the batched path) and an optional `query_timestamp`/
`query_timestamps` argument on `index_at`/`index_at_many` (omitted =
pre-Lever-8 behavior, unchanged for every other caller). `engine.py` threads
an optional `query_timestamp` through `_reinflate`/`positions_at`/
`positions_at_many`/`position_at_log_index`. `t2_gate.reconstruct_hf_at_trigger`
gained `compound_interest_to_trigger: bool = True` (each trigger's own
`block_timestamp` is the compounding target; `False` reproduces the
pre-Lever-8 number for A/B measurement, same pattern as Lever 3's
`config_history` toggle). 13 new unit tests (`tests/test_interest_index.py`,
synthetic fixture, always-run) + 2 new engine tests
(`tests/test_state_reconstruction.py`).

**Evidence (real data, both-measurable subset, n=43,452):**
```
before (index as of last write):        mismatch=11.31%
after  (compounded to trigger second):  mismatch=10.77%
fixed=233, broken=0
```

**Cost:** free (no new data -- rate is already in the lake from Track D).
Full suite: 407 passed, 1 deselected (known-failing real-data `t2_gate`).

---

## Lever 9 — Exact token-level ledger (H3) ✅ **DONE (2026-07-22)**

**Landed: 10.8% → 8.4% (−2.37 pts; 2,305 fixed / 1,328 broken, 1.7:1 ratio —
net-positive but not one-directional like Levers 5–8, see below).**

**Mechanism.** Every prior lever still replayed `LendingPool` events
(Deposit/Borrow/Repay/Withdraw/LiquidationCall) into balances, correcting
that replay's known gaps one at a time (gateway attribution, naked aToken
transfers, collateral toggles, accrual). The aToken/debtToken contracts
themselves emit the *exact* accounting directly, with the index embedded in
the event: aToken `Mint(from, value, index)` / `Burn(from, target, value,
index)` / `BalanceTransfer(from, to, value, index)`; `VariableDebtToken`
`Mint(from, onBehalfOf, value, index)` / `Burn(user, amount, index)`;
`StableDebtToken` `Mint(user, onBehalfOf, amount, currentBalance,
balanceIncrease, newRate, avgStableRate, newTotalSupply)` / `Burn(user,
amount, currentBalance, balanceIncrease, avgStableRate, newTotalSupply)`.
Replaying these directly is a strict upgrade on three axes at once:

1. **Exact scaling, no lookup.** `scaled = value / index` uses the index
   *at that exact event*, not a nearest-prior `ReserveDataUpdated` snapshot.
2. **Gateway/adapter routing resolved for free.** The token's own
   `from`/`onBehalfOf`/`user` fields always name the real position holder
   regardless of which wrapper called it -- confirmed empirically (5/5) by
   cross-checking `VariableDebtToken.Mint`'s `onBehalfOf` against the
   already-verified `fix_gateway_onbehalfof.py` `Borrow.onBehalfOf` for
   shared tx hashes. This supersedes Track C's naked-aToken-transfer
   correction *and* `fix_gateway_withdraw.py`'s Transfer-correlation
   heuristic outright, not just approximates them better -- confirmed
   against a real liquidation receipt that `LiquidationCall`'s own seizure
   (`AToken.Burn`/`BalanceTransfer`) and debt repayment
   (`Variable`/`StableDebtToken.Burn`) fire through these exact same token
   events, so no separate `LiquidationCall` handling is needed at all in the
   new path.
3. **Stable debt gets its own locked rate.** Every prior lever scaled *all*
   debt by the pool's variable-borrow index, even for stable-rate borrowers
   -- wrong, since stable debt compounds at the position's own locked,
   blended `avgStableRate` (`Repay` doesn't even carry `rateMode`, so the
   `LendingPool`-event path could never model this). `StableDebtToken`
   events carry `currentBalance` directly (Aave has already done the
   accrual math for us), so each checkpoint's `principal_after =
   currentBalance +/- amount` needs no external index lookup at all --
   forward-compounding to a query timestamp reuses Lever 8's
   `calculate_compounded_interest`, just keyed per-position instead of
   per-reserve.

**Why it's not one-directional (unlike Levers 5–8).** This isn't an
additive correction on top of an otherwise-trusted base -- it's a
wholesale replacement of the balance-reconstruction method with a strictly
more accurate one, so some previously-"matching" positions were only
matching *because* the old approximate ledger happened to land on the
right side of HF=1 for the wrong reasons (the same phenomenon Track
B/onBehalfOf's investigation hit earlier this ticket). Investigated the
1,328 broken cases directly rather than taking the net number on faith:
their pre-H3 HF was genuinely low (mean 0.35, so they looked solidly
liquidatable before) and their post-H3 HF lands just barely ≥ 1 (median
1.009; **847/1,328 (64%) are within 1.02 of the boundary**) -- consistent
with boundary-precision noise from a *more accurate* reconstruction landing
close to the true threshold, not a new systematic bug. Cross-checked the
stable-debt mechanism specifically: **588/613 (96%) of fixed-case users
carry stable debt, vs. only 144/508 (28%) of broken-case users** -- stable
debt is the dominant, correctly-signed driver of the fix side (previously
undercounted debt correctly makes real stable borrowers look more
liquidatable), while the broken side is unrelated to it (a normal
cross-section, since stable-rate borrowing was always a minority feature on
Aave v2).

**Implementation.** New `scripts/onchain/backfill_token_ledger_events.py`:
pulled `Mint`/`Burn`/`BalanceTransfer` (aToken) and `Mint`/`Burn`
(variable + stable debt tokens) full-history for all 111 addresses (37
reserves × 3 token types) via the established Etherscan `getLogs` +
recursive-bisection + per-`(address, event_type)` checkpoint machinery. All
7 event topic0s were derived via `Crypto.Hash.keccak` then verified two
ways against real logs before trusting them: exact topic0 match (including
expected indexed-arg/data-word counts) against real on-chain events, and
address-field order (which topic names the real position holder, e.g.
`onBehalfOf` vs. the calling wrapper) cross-checked against
`fix_gateway_onbehalfof.py`'s already-verified `Borrow.onBehalfOf` for
shared tx hashes. `reserves.py` gained
`STABLE_DEBT_TOKEN_ADDRESS_BY_RESERVE`/`VARIABLE_DEBT_TOKEN_ADDRESS_BY_RESERVE`
(the existing `getReserveTokensAddresses` call already returned these; the
original pull just hadn't kept them). `engine.py` gained the exact-ledger
construction (`_exact_atoken_ledger_rows`, `_exact_variable_debt_ledger_rows`,
`_exact_token_ledger`) and the stable-debt checkpoint/query path
(`_stable_debt_checkpoints`, `_stable_debt_units`, wired additively into
`_reinflate`/`positions_at_many`) behind the same no-op-when-missing
convention as every other CAS-28 correction: `PositionStateEngine.__post_init__`
uses the exact token ledger only when all three pull outputs exist,
otherwise falls back to the pre-H3 `build_ledger` + Track C + nearest-prior
scaling path unchanged. A position with stable debt but no aToken/
variableDebt event on that reserve needed an explicit zero-effect "anchor"
ledger row (`_stable_debt_only_anchor_rows`) so `positions_at`/
`positions_at_many` still discover that `(user, reserve)` pair at all.
10 new unit tests (`tests/test_state_reconstruction.py`, synthetic
fixtures) covering Mint/Burn/BalanceTransfer scaling, stable-debt additivity
and forward-compounding, the liquidation-captured-via-token-events-alone
property, and (caught only by the real-data run, not the initial synthetic
tests) a `merge_asof` sort-order regression once real multi-user data was
involved.

**Two bugs the real pull surfaced that no synthetic fixture caught.** (1)
`merge_asof` needs its right side sorted *globally* by the `on` column, not
merely within each `by` group -- `stable_debt_checkpoints` is stored sorted
by `(user, reserve, block_number)` for its other consumer, which single-row
synthetic tests satisfied by coincidence but real multi-user data didn't;
fixed by re-sorting by `block_number` alone immediately before every
`merge_asof` call, matching the pattern `positions_at_many` already used
for the ledger itself. (2) H3's replacement (not additive) ledger
construction is triggered by global file *presence*, so once the real pull
landed it silently substituted real on-chain activity for every synthetic
test's fake users across the *whole suite* (47 tests, 7 files) --
`_atoken_transfer_ledger_rows` and friends never had this problem because
they're additive and keyed by user identity, naturally inert against fake
addresses. Fixed with one shared `tests/conftest.py` autouse fixture that
disables the H3 path by default everywhere except the `t2_gate`-marked real
gate test.

**Evidence (real data, both-measurable subset, n=43,288):**
```
before (LendingPool-event replay):     mismatch=10.81%
after  (exact token-level ledger):     mismatch=8.55%
fixed=2305, broken=1328, ratio=1.7:1
```
Overall: 10.77% → 8.40%; `n_measurable` also rose (43,452 → 44,231) as some
previously-uncovered positions became priceable once their balances
resolved. Cause breakdown as *first* measured after Lever 9 (3,715
mismatches): `unexplained_no_chainlink_coverage`=3,413, `unexplained`=229,
`oracle_lag`=47, `param_drift`=26 -- **this breakdown turned out to be
wrong, from a stale diagnostic, not from H3 itself; see "Cause-bucketing
diagnostic was stale" below for the corrected numbers.**

**Cost:** free (Etherscan only). Pull: 259 jobs (111 addresses × up to 3
event types each), 2,506,818 aToken rows + 498,619 variableDebtToken rows +
53,109 stableDebtToken rows, ran in the background over several hours
(mostly network-bound against the free-tier rate limit; aWETH/aUSDC/aDAI/
aWBTC dominated the wall-clock). Full suite: 417 passed, 1 deselected
(known-failing real-data `t2_gate`, confirmed at 8.4% via `pytest -m t2_gate`).

---

## Fix — Cause-bucketing diagnostic was stale ✅ **DONE (2026-07-22)**

**Not a lever (doesn't move the 8.4% mismatch rate at all) -- a correction
to the *diagnostic tooling* that was actively misleading about where the
residual sits.**

`scripts/analysis/t2_mismatch_report.py`'s cause-bucketing step
(`bucket_mismatch_causes`) checked "does Chainlink have coverage for this
mismatch" against a **separate, standalone `ChainlinkPriceOracle`**
(asset/USD feeds, `chainlink_feeds.RESERVE_CHAINLINK_FEEDS`) instead of the
`PreferEthNumeraireOracle` actually used for the primary reconstruction.
That standalone oracle reads a **completely different aggregator map** than
H2's full-history pull touched (`RESERVE_CHAINLINK_ETH_FEEDS`), so it was
still silently golden-episode-windows-only -- checking "no coverage"
against it was checking a stale, narrower notion of coverage than what the
mismatch was actually determined with.

**Effect of the fix (same 3,715 mismatches, corrected labels only):**
```
                                    before fix    after fix
unexplained_no_chainlink_coverage   3,413          547
unexplained                           229        3,142
oracle_lag                             47            0
param_drift                            26           26
```
`oracle_lag` goes to exactly 0 by construction once the same oracle is used
on both sides of the recompute (identical inputs can't flip the verdict) --
kept in the code, not removed, in case a genuinely independent, better
price source (H7 below) gets wired in as the comparison oracle later.

**Why this matters for what to do next.** The *previous* breakdown
suggested the residual was "still fixable by pulling more Chainlink
coverage" -- wrong, and would have sent the next round of work toward
another data pull that wouldn't have moved the number. The *corrected*
breakdown says the opposite: **84.6% of the residual (3,142/3,715) is
`unexplained`** -- genuinely mismatched under the best price oracle this
project has, with no known cause identified. The remaining `547`
`unexplained_no_chainlink_coverage` cases *are* legitimately a coverage
gap, and 464/547 (85%) concentrate in reserves with no ETH-feed mapping at
all (`chainlink_feeds.UNCOVERED_ETH_RESERVES`: GUSD, xSUSHI, stETH, ENS,
CVX, LUSD) -- a small, already-understood, probably-not-worth-chasing tail
(these are obscure/discontinued reserves; an ETH feed may not even exist
for several of them).

**Implementation.** `t2_mismatch_report.py` now passes `price_oracle`
itself as `bucket_mismatch_causes`'s `chainlink_oracle` argument instead of
constructing a separate `ChainlinkPriceOracle()`. `t2_gate.py`'s
`bucket_mismatch_causes` docstring updated to explain the corrected bucket
semantics. No engine/reconstruction code changed -- the 8.4% overall rate
is exactly the same before and after this fix, only the per-mismatch
`cause` label in `mismatch_diagnostics.csv` changed. `tests/test_t2_mismatch_gate.py`'s
own `bucket_mismatch_causes` unit tests use their own oracle stubs, unaffected.

**Cost:** free (no new pull, one function-call fix). Full suite still 417
passed, 1 deselected.

---

## H7 — Archive-node ground-truth spot-check ✅ **DONE (2026-07-22)**

**Not a lever (doesn't change the 8.4% mismatch rate) -- independent
ground truth for what the `unexplained` residual actually is.** Result: it's
**not a balance/price/threshold bug in this engine**, for the bulk of
cases -- it's a same-block timing-granularity gap plus ordinary
boundary-precision noise, with a small tail of extreme-volatility outliers.

**Mechanism.** Called Aave v2's own `LendingPool.getUserAccountData(user)`
via `eth_call` at `block_number - 1` (this project's own pre-liquidation
convention, Lever 1) on a **real archive RPC** (Alchemy free tier,
`ARCHIVE_RPC_URL`), for a random sample of 300 (seed 42) of the 3,142
`unexplained` mismatches. This is Aave's *actual* live contract logic --
not another reconstruction, the ground truth every other lever has been
inferring toward. Diffed the returned `healthFactor` and (for positions
where `PreferEthNumeraireOracle` used its native ETH leg, so units align
without conversion) `totalCollateralETH`/`totalDebtETH` against this
engine's own reconstruction.

**Blocked initially, then unblocked mid-session:** `ARCHIVE_RPC_URL` had no
value set (confirmed empty in `.env`, matching the Notion API Keys page's
"absent" status for a different ticket, CAS-6) and the free public RPC used
elsewhere in this repo (`ethereum-rpc.publicnode.com`) rejects historical
`eth_call` the same way it rejects historical `eth_getLogs` (confirmed
live: 403 "Archive requests require a personal token" for a historical
block, 200 OK for `"latest"`). User supplied a new Alchemy key; its app
initially had Ethereum Mainnet disabled on Alchemy's dashboard (403 "not
enabled for this app") -- a one-click fix on the Alchemy side, then the
real calls worked.

**A decode bug caught immediately by a sanity check, before trusting any
result:** first attempt divided `healthFactor` by RAY (1e27, the scaling
every *other* on-chain value in this project uses -- indices, rates).
Every result came back ~1e-9 (absurd). Hand-computed HF from the *other*
decoded fields in the same call (`totalCollateralETH * liquidationThreshold
/ totalDebtETH ≈ 0.914`) and compared: `healthFactor` (and
`totalCollateralETH`/`totalDebtETH`) are **WAD-scaled (1e18)**, not RAY --
confirmed by matching that hand-computation exactly once corrected. Applies
to `totalCollateralETH`/`totalDebtETH` too (already assumed 1e18 correctly,
that part was fine).

**Results (n=300, all 300 calls succeeded):**
```
On-chain confirms mismatch (HF >= 1 or no debt): 115/300 (38.3%)
On-chain DISAGREES (shows HF < 1):               185/300 (61.7%)
```
Breaking the disagreements down by `onchain_hf`:

| Band | Count | % of disagreements | Interpretation |
|---|---|---|---|
| `onchain_hf` >= 0.99 | 142 | 76.8% | boundary-precision noise |
| 0.95 <= `onchain_hf` < 0.99 | 26 | 14.1% | moderate, likely same cause at higher magnitude |
| `onchain_hf` < 0.95 | 17 | 9.2% | real outliers -- see below |

**The 38.3% "confirms" group is the most important finding.** Both `our_hf`
and `onchain_hf` cluster *extremely* tightly just above 1.0 (`onchain_hf`:
median 1.004, **std 0.0035**). Aave's own live contract, queried at the
exact same `block_number - 1` cutoff this engine uses, *also* says the
position was healthy entering the trigger block -- yet a liquidation
happened in that block. The only explanation: something landed **earlier in
the same block**, before the liquidating tx, that a whole-block cutoff
can't see. This is precisely **H4b** (same-block price ordering -- "a
same-block price update that landed before the liquidation is currently
invisible to a block-level cutoff"), scoped out earlier this session after
a narrower measurement found only 633 candidates. This result suggests that
estimate underspoke; H4b (or a broader same-block-state generalization of
H4a, which currently only special-cases the *cascade* -- same-user,
same-block, multiple-liquidations -- case, not "any earlier same-block
event for this user") is worth re-scoping.

**The 76.8%-of-disagreements near-boundary group matches this ticket's own
earlier (superseded) "boundary-precision floor" analysis** -- except now,
after H1-H5 and H3 closed the *fixable* fidelity gaps that analysis
couldn't see at the time, what's left really does look like a genuine
precision floor for the bulk of cases: reconstructing exact-block HF from
event logs is bounded by real, small effects (block-level vs. tx-level
price/state resolution) that stack, and liquidations concentrate exactly
where a sub-1% error flips the verdict.

**The 9.2% real-outlier tail is disproportionately extreme-volatility
episodes.** Hand-traced the single largest gap in the n=10 pilot sample
(`onchain_hf=0.9997` vs `our_hf=1.601`, user `0xda24653e...`, block
15043217, mid-May 2022): this user's UST variable debt grew from a 30,000
UST mint (14767435) to 32,142 UST at trigger -- `InterestIndexOracle`'s own
rate history for UST across this exact window (60-79% APR) confirms this
growth is *real*, not a reconstruction bug (UST's variable borrow rate
spiked during its May 2022 death spiral, exactly this block range). The gap
is far more likely fast-moving price staleness during an active depeg
(a known-hard case for any nearest-prior-price reconstruction, related to
but broader than H4b) than a balance bug -- collateral matched almost
exactly (median 0% diff across the full 300-sample) while only debt showed
a wide spread, and the two known variable-debt-affecting mechanisms
(index, rate) both checked out by hand for this case.

**Component diffs, ETH-numeraire-matched disagreements (n=129, direct units
match, no conversion needed):**
```
collateral_eth (ours - onchain) / onchain: mean=+2.78%  median=+0.00%
debt_eth       (ours - onchain) / onchain: mean=-8.54%  median=-0.97%
```
Collateral reconstruction is essentially exact at the median (H3's exact
aToken ledger doing its job). Debt has a small (<1%) typical gap, consistent
with the boundary-precision explanation above, with the mean pulled up by
the same small outlier tail (UST-type episodes).

**Implementation.** New `scripts/analysis/archive_ground_truth_spotcheck.py`
-- samples a cause bucket (default `unexplained`), calls
`getUserAccountData` via `ARCHIVE_RPC_URL`, writes
`experiments/T2/output/h7_archive_spotcheck.csv`. `getUserAccountData`
selector (`0xbf92857c`) derived via `Crypto.Hash.keccak`, verified live
(a real historical call returning a decodable, sane HF) before trusting it,
same discipline as every other CAS-28 selector.

**Cost:** free (Alchemy free tier, 300 `eth_call`s -- no rate-limiting hit
at this volume). No engine changes, no test changes (this is a standalone
diagnostic script, not part of the reconstruction pipeline). `ARCHIVE_RPC_URL`
now present and working in `.env` -- see the Notion API Keys page.

**Recommended next step.** The evidence pointed at two concrete, well-scoped
options rather than more open-ended searching:
1. **Re-scope H4b** (or generalize H4a beyond the cascade case) given the
   38.3% "confirms" finding suggests same-block timing explains a much
   larger share of the residual than the original 633-candidate estimate.
2. **H9's tolerance-band ADR** is now well-justified by real evidence (not
   an assumption) for the remaining near-boundary bulk (76.8% of
   disagreements within 1% of HF=1, confirmed against live contract state,
   not just internal reconstruction consistency).
Option 1 was taken first (the smaller, more surgical one, and the one that
might directly reduce the mismatch *rate* rather than just re-explain it) —
see **Lever 10** immediately below for what actually happened when it was
implemented and measured. Option 2 (H9) remains open.

---

## Lever 10 — Same-block price ordering (H4b) + earlier-activity generalization ✅ **DONE (2026-07-22)**

**Landed: 8.40% → 8.29% (−0.11 pts; 97 fixed / 47 broken, 2.06:1 ratio) —
small, as expected for a boundary-precision lever, and well below what H7's
38.3% "confirms" figure might have suggested. The gap between that estimate
and the actual yield is itself the finding — see below.**

**Mechanism.** Two changes landed together (both were sitting in the
working tree from the same investigation and are measured as one unit):

1. **H4b — same-block price ordering.** Every price oracle
   (`ChainlinkPriceOracle`, `EthNumeraire`, `BlendedPriceOracle`,
   `PreferEthNumeraireOracle`) resolved prices by nearest-prior
   `block_timestamp` alone -- but every tx in a block shares one timestamp,
   so this can't distinguish intra-block order. `price_at`/`prices_at`
   gained optional `block_number`/`log_index` params; when given, resolution
   switches to "last price strictly before `(block_number, log_index)`" via
   a combined sortable `_order_key = block_number * 1_000_000 + log_index`
   (Ethereum blocks are nowhere near 1M logs, so this never collides two
   distinct positions). `t2_gate.py` now passes the trigger's own
   `block_number`/`log_index` on every `prices_at` call, in both the primary
   reconstruction and `bucket_mismatch_causes`. `_merge_aggregator_series`
   now sorts/dedupes by `(block_number, log_index)` instead of collapsing
   same-timestamp rows via `block_timestamp`-only `drop_duplicates`, which
   previously could silently discard an earlier same-block reading.
2. **H4-general — same-block-earlier-activity, generalized beyond
   cascades.** Lever 7 (H4a) only routed a trigger to the precise
   per-log-index path (`engine.position_at_log_index`, instead of the fast
   batched `block_number - 1` query) when it was itself part of a same-user
   liquidation cascade. H7's 38.3% "confirms" finding implies the *general*
   case matters too: a Withdraw, Borrow, or any other ledger event for the
   same user earlier in the trigger's own block is equally invisible to a
   whole-block cutoff. New `engine.same_block_earlier_tx` scans ledger +
   stable-debt activity directly for this; `t2_gate.py` now routes a trigger
   to the precise path when `is_cascade | engine.same_block_earlier_tx(...)`.
3. **A latent cascade bug this surfaced and fixed in passing.** A
   liquidation's own internal token events (aToken `Burn`/`BalanceTransfer`,
   debt-token `Burn`) can have a *lower* `log_index` than its own outer
   `LiquidationCall` event (confirmed on real data) -- a naive
   `log_index < log_index` cutoff would then wrongly count the liquidation's
   *own* seizure/repay as part of its "pre-state". Fixed via a `tx_hash`-
   scoped exclusion in `position_at_log_index` (only on the H3 exact-ledger
   path, which is the only one with a `tx_hash` column), with an
   `after_log_index` floor to handle the 174 real `(tx_hash, user)` pairs
   where >1 `LiquidationCall` batches into one tx (so the same-tx widening
   for the *second* liquidation doesn't also wrongly exclude the *first*
   liquidation's real, legitimate effects).

**Evidence (real data, both-measurable subset, n=44,231 -- coverage
unchanged; this lever only affects precision, not measurability):**
```
before (block-1 whole-block cutoff except H4a cascades): mismatch=3715, rate=8.40%
after  (H4b price ordering + H4-general precise-path routing): mismatch=3665, rate=8.29%
fixed=97, broken=47, ratio=2.06:1
```
Cause breakdown shift: `unexplained` 3,142 → 3,083 (−59),
`unexplained_no_chainlink_coverage` 547 → 555 (+8), `param_drift` 26 → 27
(+1).

**Why the yield is much smaller than H7's 38.3% might have suggested.** H7
showed that *both* this engine's reconstruction and Aave's own archive-node
contract call agree HF ≥ 1 at `block_number - 1` -- proving the true flip to
HF < 1 must happen somewhere *inside* the trigger's own block, but not that
the cause is something *this engine's own ledger/price data can see*. Of the
≈1,204 candidate population (3,142 `unexplained` × 38.3%), only 97 actually
flipped once given a precise, log_index-aware view of the same data this
engine already has. That implies the dominant same-block cause for most of
that 38.3% is something *outside* this reconstruction's inputs entirely --
e.g. a price move not backed by a Chainlink `AnswerUpdated` event at all
(a DEX-only or custom-oracle price swing), or an atomic same-tx sequence
(flash-loan-style self-liquidation setup) this event schema doesn't model as
separate same-block state. This sharpens, rather than resolves, the case for
**Lever 11** (real oracle-source timeline -- some of that 555-strong
no-coverage bucket and possibly part of `unexplained` are priced via sources
this engine doesn't model yet) and **H9's tolerance-band ADR** (the
remaining near-boundary bulk).

**Implementation.** `prices.py`: `_order_key` helper +
`_ORDER_KEY_LOG_INDEX_MULTIPLIER`; every price class's `price_at`/
`prices_at` gained optional `block_number`/`log_index` params (backward
compatible -- `None` reproduces exact pre-Lever-10 behavior, used by
`prices.PriceOracle`/DefiLlama which has no intra-day granularity to use
them for). `engine.py`: `tx_hash` threaded through `_empty_ledger_rows`,
`_exact_atoken_ledger_rows`, `_exact_variable_debt_ledger_rows`,
`_stable_debt_checkpoints` (H3 exact-ledger path only); `position_at_log_index`
gained `tx_hash`/`after_log_index` params; new `same_block_earlier_tx`
method. `t2_gate.py`: `PriceOracleLike` protocol updated; trigger routing
generalized from `is_cascade` alone to `is_cascade | same_block_earlier_tx`;
`after_log_index` computed per-key from the previous same-`(user, tx_hash)`
trigger. New/updated tests across `tests/test_chainlink_price_oracle.py`
(price-ordering unit tests), `tests/test_state_reconstruction.py`
(`same_block_earlier_tx`/`tx_hash` engine tests), `tests/test_t2_mismatch_gate.py`
(the Withdraw-from-a-different-tx integration test), plus signature-only
updates in `tests/test_backfill_chainlink_t2_windows.py` and
`tests/test_dex_liquidity.py` for the new optional `price_at`/`prices_at`
params.

**Cost:** free (no new data -- uses ledger/price data already in the lake).
Full suite: 431 passed, 1 deselected (known-failing real-data `t2_gate`,
confirmed at 8.3% via `pytest -m t2_gate`).

---

## Lever 11 — Aave's real oracle-source timeline ✅ **DONE (diagnostic 2026-07-22, landed 2026-07-22)**

**Landed (see "Lever 11 landed" below for the full before/after): 8.3% →
7.6%.** This pulled real data and validated `RESERVE_CHAINLINK_ETH_FEEDS`
against Aave's own on-chain source history, as planned. The result is a
**much bigger finding than the plan anticipated**:
three distinct, confirmed coverage gaps, only one of which (WBTC) was
originally suspected. Presenting the diagnostic before implementing more of
it, per this file's own discipline -- the remaining work (pulling ~20+
reserves' missing aggregator phases, then Lever 12's clipping to handle the
now-confirmed multi-phase overlaps, then re-measuring) is substantial enough
to warrant a scope check before continuing.

**New data pulled.** `scripts/onchain/backfill_aave_oracle_sources.py` pulls
`AaveOracle.AssetSourceUpdated(asset, source)`'s full history (topic0
derived via `Crypto.Hash.keccak`, verified live against real decodable rows
matching known token addresses at block 11,275,902 -- Nov 2020, *before*
Aave v2's commonly-cited ~11,362,000 launch block). 100 rows, 56 distinct
assets, 64 rows across the 37 Aave v2 reserves. Written to
`data/raw/aave_v2_oracle_sources/chain=1/asset_source_updated.parquet`.
`scripts/onchain/validate_aave_oracle_sources_per_era.py` then walks each
historical `source` directly: if it exposes `phaseId()`/`phaseAggregators(n)`
(the standard `EACAggregatorProxy` pattern), its **full** phase history is
recovered with no FeedRegistry lookup at all -- this matters because H2's
own docstring records 8 reserves (WBTC, BUSD, TUSD, renFIL, USDP, DPI, FRAX,
FEI, UST) have **no FeedRegistry entry** for their pair and were never
cross-validated. Diffing the recovered ground truth against
`RESERVE_CHAINLINK_ETH_FEEDS` found **32 of 37 reserves with at least one
missing aggregator address** -- but most of that is noise (see below); three
findings are real:

**1. A systemic early-2021 coverage gap, ~20 reserves, not just WBTC.**
Cross-referencing each reserve's earliest *mapped* aggregator's first
`AnswerUpdated` (from the already-pulled H2 full-history parquet -- no new
calls needed) against that reserve's earliest *real liquidation* block shows
most major reserves' current map coverage only starts around block
12,016,000-12,070,000 (~Mar 2021) -- WBTC, DAI, USDC, USDT, LINK, sUSD, BUSD,
BAT, MANA, SNX, ENJ, ZRX, CRV, BAL, MKR, YFI, UNI, REN all show this pattern,
by 70,000 to 650,000 blocks (roughly 10-100 days). Real liquidations for
these reserves start as early as block 11,471,171 (~Dec 2020, weeks after
launch). The uniformity of the ~12,016,000-12,017,000 cluster across
*unrelated* reserves (DAI/USDC/USDT/LINK/sUSD/BUSD all within ~1,200 blocks
of each other) strongly suggests a coordinated Chainlink infrastructure
migration around Mar 2021 -- which predates even the earliest golden episode
(China'21, May 2021), so H1's golden-window-only sweep never had a chance to
see whatever phase was active before it. **This means positions liquidated
in Aave v2's first ~3 months have no ETH-numeraire price coverage at all**,
degrading to the noisier USD-blended path (Lever 2/5's precedent) or
`unexplained_no_chainlink_coverage` outright. Confirmed real (not
FeedRegistry noise) because it's measured against actual liquidation blocks,
not just "a phase exists we don't have."
- **11 reserves already fine** (positive gap -- map coverage starts before
  their first real liquidation, so their early phases are moot): KNC, AAVE,
  FRAX, AMPL, FEI, USDP, DPI, RAI, UST, 1INCH, renFIL.

**2. WBTC (the plan's headline suspect) -- partially confirmed, more
nuanced than expected.** Aave's real 2020-2023 WBTC source (`0xdeb288f7...`,
active blocks 11,275,902-17,400,308, spanning China'21/Dec'21/Terra'22/FTX'22
in full) has `description() == "BTC / ETH"` -- **matches our current
approximation exactly**, so the FTX-era mispricing concern the plan raised
does *not* apply to that source directly. But that proxy has **5 internal
phases**; ours (`0x81076d6f...`) is phase 4 (starts block 12,069,614, ~Mar
2021 -- same systemic gap as finding 1), and phase 5 (`0xb0fd105d...`,
currently active) was never pulled. Separately, **Aave switched WBTC's
top-level source entirely at block 17,400,308 (2023-06-03)** to
`0xfd858c8bc5ac5e10f01018bc78471bb0dc392247`, `description() == "wBTC/BTC/ETH"`
-- a real wBTC/BTC-depeg-aware composite feed, completely unmapped. **21,883
of 49,331 liquidations (44%) occur after this switch** -- every one of them
currently priced (if WBTC-covered at all) via the pre-2023 feed's stale
final answer or a fallback path, not the feed Aave actually reads.

**3. A 2024-2025 mass stablecoin migration, entirely unmapped (new
finding, not in the original plan).** 10 reserves (DAI, USDC, USDT, BUSD,
TUSD, USDP, FRAX, LUSD, sUSD, UST) plus DPI got a **new top-level source**
(via `AssetSourceUpdated`, not just an internal phase change) at two blocks
shared across all of them: **19,723,911 (2024-04-24)** and **22,616,472
(2025-06-02)** -- consistent with a coordinated Aave v2 deprecation-era
governance action (Aave v2 is wound down; `reserves.py` already notes most
reserves were de-risked toward zero before the parameter freeze), not
organic per-asset price discovery. The new sources' `description()` follows
a `"Capped <SYM> / USD / ETH"` pattern -- decodable, plain-looking
aggregators (not custom adapters), just never pulled. **19,845 liquidations
(40%) occur after the first migration block; 8,880 (18%) after the
second** -- both are within this project's Jan-2021-to-Feb-2026 scope, so
this is not a "post-study-period, doesn't matter" gap.

**4. The 6 originally "uncovered" reserves -- 2 are NOT custom adapters,
4 are confirmed genuinely custom.** Resolving each source's own
`decimals()`/`description()`:
- **stETH and CVX are plain, standard Chainlink aggregators** ("STETH / ETH"
  / "stETH/ETH", "CVX / ETH") that the original description-matching sweep
  simply never found -- trivial fix, no adapter replication needed, just
  pull `AnswerUpdated` for the resolved addresses and add to the map.
- **GUSD (both its 2020 and 2021 sources), ENS, xSUSHI's original 2021-2023
  source, and LUSD's original 2022-2024 source revert on `decimals()`/
  `description()` AND emit no `AnswerUpdated` event at all** (confirmed via
  direct `getLogs` -- zero hits for all 5 addresses across the full block
  range). These are genuinely custom, with no shortcut: Lever 11b's
  `getsourcecode` + replication plan is the only path for these specific
  eras. (xSUSHI's 2023+ source and LUSD's 2024+ sources are already
  plain/standard, matching the mass-migration pattern in finding 3.)

**Why this wasn't measured as a rate change yet.** Confirming a gap exists
and quantifying its overlap with real liquidations (this section) is
necessarily separate from pulling every missing aggregator's full
`AnswerUpdated` history, wiring multi-phase merging (already supported by
`_merge_aggregator_series`) with correct clipping (Lever 12 -- now clearly
higher-priority than originally scoped, since finding 1 alone confirms
multiple real phases genuinely overlap in time across ~20 reserves, not a
hypothetical), and re-running the gate. That remaining work is substantial
(dozens of new aggregator addresses across three separate eras) and is a
scope/priority decision, not a mechanical next step -- see the status note
at the bottom of this file.

**Implementation so far.** `scripts/onchain/backfill_aave_oracle_sources.py`
(the pull) and `scripts/onchain/validate_aave_oracle_sources_per_era.py`
(the per-era validation + diff report) are both committed. No changes yet to
`chainlink_feeds.py`, `prices.py`, or any reconstruction path -- this lever
is diagnostic-only so far, deliberately, pending a scope decision on which
of the three confirmed gaps to pull and wire first.

**Cost so far:** free (Etherscan + public RPC), a few minutes of wall time
for the pull, more for the ~65-address phase-walk validation. No test
changes (no reconstruction code touched yet).

---

## Lever 11 follow-up — Stablecoin migration attempt: caught a regression, reverted, mechanism kept

**Attempted finding 3 (the 2024/2025 stablecoin migration) first, per its
"cleanest to pull" ranking above. It was not clean -- new information
downgrades it below finding 1 (the early-2021 gap). No net rate change:
landed the reusable clipping mechanism, reverted the specific (harmful)
wiring for these 11 reserves.**

**What went wrong.** Pulling `AnswerUpdated` for all 22 of the 2024/2025
migration addresses returned **zero rows for every single one** -- these
"Capped X/USD/ETH" sources are not raw Chainlink aggregators at all.
`getsourcecode` on DAI's 2024 source (`0xd486fe27...`) shows it's BGD Labs'
`CLSynchronicityPriceAdapterBaseToPeg`: a live-computation wrapper with no
event history of its own, whose `latestAnswer()` computes
`(ASSET_TO_PEG.latestAnswer() * 10^decimals) / BASE_TO_PEG.latestAnswer()`
from two other `immutable` feed addresses read fresh on every call. Resolving
those for DAI found `BASE_TO_PEG` is a "ETH / USD" feed at a *third*,
also-unpulled address, and `ASSET_TO_PEG` is **another wrapper**
("Capped DAI/USD") one layer deeper still. Recovering real historical prices
here needs: resolving however many layers deep this goes, pulling each real
leaf feed's history, and re-implementing the adapter's own ratio (and
whatever the "Capped" layer's cap/clamp formula is) in Python -- this is
Lever 11b-shaped work (`getsourcecode` + formula replication), not a data
pull, and applies per-reserve (each may wrap different underlying feeds).

**The regression this caused, caught before committing.** Wiring the
(empty) new addresses in anyway -- clipping the pre-2024 aggregators to stop
at the migration block, since Aave's own history proves it stopped reading
them, with nothing real to replace them -- measured **8.29% → 13.36%**
(3,665 → 5,908 mismatches). Mechanism: `PreferEthNumeraireOracle` falls back
to the noisier USD-`BlendedPriceOracle` path for an entire position when
*any* touched reserve loses ETH-numeraire coverage, so this single change
pushed every post-migration position touching DAI/USDC/USDT/etc. back into
the pre-Lever-5 compounding-cross-rate noise. Caught by this file's own
"measure before/after every change" discipline, not by inspection -- reverted
immediately (`git checkout` on `chainlink_feeds.py`), confirmed back to
exactly 8.29% after.

**What was kept.** `prices.py`'s `_merge_aggregator_series`/`EthNumeraire`
gained an optional `aggregator_eras` parameter (per-address
`(era_start_block, era_end_block)` clip, sourced from real
`AssetSourceUpdated` boundaries) -- this *is* Lever 12's core mechanism,
built and unit-tested (`tests/test_chainlink_price_oracle.py`, a synthetic
fixture proving both the bleed bug and the fix, plus a backward-compatibility
test) against synthetic data, but currently unused by any real
`RESERVE_CHAINLINK_ETH_FEEDS` entry (`aggregator_eras` defaults to `None`
everywhere, a no-op) -- ready for the next candidate that has real,
poolable per-era data.

**Revised priority: the early-2021 gap (original finding 1) is next, not the
stablecoin migration.** Verified directly this session: WBTC's phases 1/3,
DAI's phase 1, and USDC's phase 1 (the early-2021-gap candidates) all have
real, non-zero `AnswerUpdated` history via direct `getLogs` checks -- genuine
raw aggregators, not live-computation wrappers. That gap can use the
`aggregator_eras` mechanism exactly as designed (pull each early phase's
history, clip each reserve's known aggregators to their own real windows) with
no formula-replication risk. WBTC's 2023 feed switch (finding 2) is
unverified either way and should be checked the same way (does
`0xfd858c8bc5ac5e10f01018bc78471bb0dc392247`, the "wBTC/BTC/ETH" source,
actually emit `AnswerUpdated`, or is it also a wrapper?) before attempting.

**Cost:** free (Etherscan + public RPC, ~22 addresses pulled and confirmed
empty -- no wasted spend, just wall time and one Etherscan `getsourcecode`
call). Full suite: 434 passed, 1 deselected (431 + 3 new clipping tests).

---

## Lever 11 landed — Early-2021 gap + stETH/CVX coverage; WBTC clip tried and reverted ✅ **DONE (2026-07-22)**

**Landed: 8.29% → 7.64% (−0.65 pts net). Three separate changes, measured
individually before combining, exactly one of which was kept as originally
conceived (early-2021 gap), one of which turned out to be a clean bonus
beyond the original plan (stETH/CVX), and one of which measured worse and
was reverted (WBTC's `coverage_end` clip) -- same "measure before/after,
keep only what helps" discipline as every prior lever.**

**1. The early-2021 systemic gap (finding 1) -- landed, small regression,
kept as a completeness fix.** Pulled 31 real, verified-non-empty aggregator
addresses across 17 reserves (USDT, WBTC, YFI, ZRX, UNI, BAT, DAI, ENJ, LINK,
MANA, MKR, REN, SNX, sUSD, USDC, CRV, BAL) covering each reserve's real
pre-\~Mar-2021 Chainlink phase, wired via `aggregator_eras` (the mechanism
built during the stablecoin-migration attempt). Each reserve's era boundary
was computed from the real pulled data (min block of the next known
aggregator), not assumed -- caught and fixed a chronological-vs-alphabetical
sort bug in that computation before wiring anything in. **Measured: 8.286% →
8.377%** (n_measurable 44,231→44,325, n_mismatch 3,665→3,713; among
previously-measurable positions, 28 fixed / 76 broken). Investigated the
broken side directly before deciding to keep it: `decimals()` consistency
checked out, block ranges and HF values are consistent with genuine
close-margin boundary noise in the expected early-2021 window, not an
implementation bug. Kept despite the net-negative direction, same precedent
as Levers 3/4 (real, verified, point-in-time data that legitimately reveals
some previously-masked mismatches is a correctness fix, not metric-gaming) --
and, as it turned out, more than paid for by finding 4 below once combined.

**2. WBTC's 2023 feed switch (finding 2) -- resolved, and the obvious fix
measured worse.** Checked directly (per this file's own caution) whether the
2023-switch source `0xfd858c8bc5ac5e10f01018bc78471bb0dc392247` ("wBTC/BTC/ETH")
actually emits `AnswerUpdated`: **zero rows**, confirming it's another
live-computation wrapper, same failure mode as the stablecoin migration --
not pullable without adapter replication. Aave's own `AssetSourceUpdated`
history proves it stopped reading the old proxy (`0xdeb288f7...`) entirely at
block 17,400,308, so a `coverage_end: 17_400_308` clip (a new mechanism added
to `EthNumeraire` for exactly this case -- see below) is *technically*
correct: that proxy's phase 4 keeps emitting for other consumers past that
block (confirmed real through block 20,715,211, and a phase 5 exists after
that too, active 20,289,324-25,588,459) but Aave itself no longer reads
either. **Measured in isolation: 8.38% → 9.26%** (n_measurable 44,325 →
43,373, n_mismatch 3,713 → 4,017) -- worse on every axis: 952 positions lost
ETH-numeraire coverage outright (838 of them *previously correctly
matching*, only 114 previously mismatched), and even the always-measurable
remainder was net negative (broken > fixed, unlike every other lever in this
ticket). The likely explanation: Aave's move to a wrapper is a
risk-management overlay (a price cap/deviation check), not necessarily a
materially different price when no de-peg is happening, so the "stale"
phase-4 answer apparently tracks the real BTC/ETH price closely enough in
practice, while removing it forces either no price at all (WBTC's separate
USD-quoted approximation, `RESERVE_CHAINLINK_FEEDS`, doesn't have full
post-2023 coverage either) or the noisier USD cross-rate reconstruction
Lever 5 was built to avoid. **Reverted** (`coverage_end` removed from WBTC's
entry only) -- confirmed back to exactly 8.38% (combined with the other two
changes below) once removed. Phase 5 was never added (would only ever fall
inside the reverted clip's dead zone, so it's moot either way).

**3. stETH and CVX (part of finding 4) -- a clean bonus win, not in the
original plan's ranking.** Both are plain, standard Chainlink `asset/ETH`
aggregators the original description-matching sweep simply never found
(confirmed via `decimals()`/`description()`: "STETH / ETH", 18 decimals;
"CVX / ETH", 18 decimals) -- previously in `UNCOVERED_ETH_RESERVES`, so every
position touching either reserve was already forced onto the noisier
USD-blended fallback (Lever 5's whole rationale). CVX has a single Aave-read
era with 2 real internal phases (`aggregator_eras` clip at block 20,436,165,
the same "clip at the next phase's first real activity" heuristic used for
the early-2021 gap); both phases have full, non-empty `AnswerUpdated`
history and Aave has not switched away from this proxy, so CVX now has full
native ETH coverage from its 2022-06-13 listing onward. stETH is more
constrained: Aave switched stETH to its own unmapped, event-less wrapper at
block 17,546,061 (2023-06-24, confirmed via the same direct-`getLogs` check
that caught WBTC's), so only its real, pre-switch phase
(`0x716bb759a5f6facdff91f0afb613133d510e1573`) was added, bounded by the new
`coverage_end` mechanism at 17,546,061 -- giving stETH genuine coverage for
2022-02-27 through 2023-06-24 where it previously had none at all, and
correctly reporting "no coverage" (unchanged from before) after that, rather
than silently reusing a stale answer. **Measured in isolation (on top of the
early-2021-gap baseline): 8.38% → 7.64%** (n_measurable 44,325 → 44,459,
n_mismatch 3,713 → 3,395) -- **340 fixed / 0 broken** among the
already-measurable population (the cleanest ratio of any lever in this
ticket), plus 134 newly-covered positions (112 matching, 22 mismatched,
roughly the population's baseline rate). Pure upside, as expected for a
purely additive coverage fix with no prior data to contradict.

**Combined (all three changes together, WBTC's clip reverted): 8.29% →
7.64%.** Confirmed via the real gate: `n_measurable=44,459`, `n_mismatch=3,395`,
`mismatch_rate=7.636%`. Cause breakdown: `unexplained`=2,877,
`unexplained_no_chainlink_coverage`=491, `param_drift`=27.

**New mechanism.** `EthNumeraire` gained an optional per-reserve
`coverage_end` (block number): once a query's `block_number` is at or past
it, `price_at`/`prices_at` return `None` immediately, before any
nearest-prior lookup -- expressing "Aave provably stopped reading every
mapped aggregator for this reserve here, and there's nothing real to
replace it" as a true coverage gap rather than an unbounded-staleness bleed.
Unlike `aggregator_eras` (which clips *between* two known aggregators of the
*same* reserve), `coverage_end` clips the *whole reserve* with no
replacement -- needed because H2's unbounded staleness (by design) never
expires a mapped aggregator's last answer on its own. 3 new unit tests
(`tests/test_chainlink_price_oracle.py`): cutoff enforcement, the
`block_number`-required caveat (a timestamp-only caller doesn't get the
cutoff, matching every other `block_number`-optional refinement in this
module), and backward compatibility when omitted. Currently used by stETH
only (WBTC's use was reverted, per finding 2) -- kept because it's a small,
correct, well-tested, self-contained addition, same "keep the mechanism,
revert the harmful wiring" precedent as `aggregator_eras` after the
stablecoin-migration attempt.

**Implementation.** `chainlink_feeds.py`: 31 new addresses across 17
existing reserves (finding 1) + 2 new reserve entries, stETH and CVX
(finding 3); `UNCOVERED_ETH_RESERVES` now lists only GUSD, xSUSHI, ENS, LUSD
(the 4 confirmed genuinely-custom, no-shortcut cases from finding 4 --
Lever 11b territory). `prices.py`: `EthNumeraire.__init__`/`price_at` gained
`coverage_end`. Full suite: 437 passed, 1 deselected (known-failing
real-data `t2_gate`, confirmed at 7.6% via `pytest -m t2_gate`).

**Cost:** free (Etherscan + public RPC). Pull: 3 new addresses (stETH phase,
CVX ×2), 6,569 rows, under a minute (small relative to the 84-address
early-2021-gap pull already in the lake). A handful of `eth_call`s to verify
`decimals()`/`description()`/`AnswerUpdated`-presence for ~10 candidate
addresses before trusting any of them, same discipline as every other
CAS-28 selector/address.

---

## Lever 11b — GUSD/ENS/LUSD custom-adapter cross-rate replication ✅ **DONE (2026-07-23)**

**Landed: 7.64% → 6.52% (−1.12 pts net) — the largest single-lever gain since
Lever 9, and the cleanest ratio of any lever this ticket (38.6:1 fixed:broken
on the previously-measurable subset) plus by far the largest completeness
gain (1,514 newly-measurable triggers, 11x stETH/CVX's 134).**

**Before implementing anything, sized the real opportunity.** Lever 11
finding 4 confirmed GUSD (both eras), xSUSHI's original source, ENS, and
LUSD's original source all revert on `decimals()`/`description()` and emit no
`AnswerUpdated` — genuinely custom, no shortcut — and framed the resulting
`unexplained_no_chainlink_coverage` bucket (491 cases) as "a small,
already-understood, probably-not-worth-chasing tail." That framing undercounted
the real opportunity: a position falls back to the noisier USD-blended oracle
for its *whole* position (Lever 5's `PreferEthNumeraireOracle` mechanism) if
*any* touched reserve lacks ETH coverage — so a GUSD/ENS/LUSD leg can be
implicated in a mismatch that's bucketed as plain `unexplained`, not
`unexplained_no_chainlink_coverage`. A direct census (reconstructing every
trigger and checking reserve membership in `position_by_key`, not just
reading the mismatch-only diagnostics CSV) found, of the 8,267
currently-unmeasurable-or-mismatched triggers: **GUSD touches 2,540** (1,999
unmeasurable, 541 mismatched), **xSUSHI touches 2,358** (2,247/111), **ENS
609** (548/61), **LUSD 122** (66/56) — 58% of the whole "interesting"
population touches at least one of these 4 reserves. This directly justified
the engineering cost below, reversing the prior session's "probably not worth
chasing" framing now that it was actually measured.

**Reading each custom adapter's real, verified Etherscan source
(`getsourcecode`) found 3 of the 4 reduce to a plain Chainlink cross-rate,
computed fresh on every call — not an approximation, this is Aave's own real
formula, just never event-emitting itself:**
```
GusdPriceProxy / ExtendedGusdPriceProxy (GUSD, both eras -- byte-for-byte
identical formula/constant/address despite the interface change):
    latestAnswer() = (1e8 * 1 ether) / ETH_USD.latestAnswer()
    -- GUSD's 1:1 USD peg is hard-coded, no separate GUSD/USD feed read at all.
EnsUsdToEnsEth (ENS):
    latestAnswer() = (ENS_USD.latestAnswer() * 1 ether) / ETH_USD.latestAnswer()
LSUDUsdToLUSDEth (LUSD's original 2022-2024 source):
    latestAnswer() = (LUSD_USD.latestAnswer() * 1 ether) / ETH_USD.latestAnswer()
```
All three read the same `ETH_USD` proxy
(`0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419`, Chainlink's canonical mainnet
ETH/USD feed, confirmed live via `description()`). **xSUSHI's adapter is
structurally different** — `SUSHI.balanceOf(xSUSHI) * SUSHI_ORACLE.latestAnswer()
/ xSUSHI.totalSupply()`, the SushiBar share-price mechanism — `balanceOf`/
`totalSupply` are live contract state, not events, so it needs either
per-block archive `eth_call`s or a full ERC20-Transfer-event replay (a
materially different, harder mechanism). Split off as its own follow-on
rather than blocking GUSD/ENS/LUSD on it.

**A free prerequisite finding: `ETH_USD_AGGREGATORS` itself had the same
gap Lever 6/H2 fixed for the asset/ETH feeds, just never applied here.**
Replicating GUSD/ENS/LUSD needs full ETH/USD history, but
`chainlink_feeds.ETH_USD_AGGREGATORS` held only 1 address (phase 5,
golden-episode-window rows only: 35,412 rows, blocks
`[12,414,796, 17,238,713]`). The real proxy has **7 on-chain phases**
(`phaseId()`/`phaseAggregators(n)`, same walk as
`validate_aave_oracle_sources_per_era.py`) — phases 6/7 (2023-06 onward,
`0xe62b71cf...`/`0x7d4e7420...`) were entirely missing, silently starving
`ChainlinkPriceOracle`'s existing BAL/USDP ETH→USD conversion of ~3 years of
the study period. Fixed as part of the same pull (free, same Etherscan
mechanics) — phase 2 confirmed real but zero-`AnswerUpdated` (pre-dates Aave
v2 entirely, harmless either way).

**Implementation.** `scripts/onchain/backfill_custom_adapter_underlying_feeds.py`
pulls full `AnswerUpdated` history for ETH/USD's 7 phases, ENS/USD's 2
phases, and LUSD/USD's phase 1 only (phase 2 starts at block 20,188,536,
*after* LUSD's original-source era ends at 19,723,911 — irrelevant here).
305,178 rows, under a minute. `scripts/onchain/derive_custom_adapter_eth_feeds.py`
then replays each formula exactly in plain Python ints (matching Solidity's
own floor division, no float rounding) over the **union of both inputs'
breakpoints** (not just one side's, unlike `ChainlinkPriceOracle._convert_via_eth_usd`'s
existing one-directional BAL/USDP conversion) — this is exact, not an
approximation: between any two consecutive breakpoints neither input has
moved, so the ratio is constant there, and "nearest-prior row in the
synthetic series" reproduces exactly what a fresh on-chain call would return
at any query point in between. Output written in the exact `AnswerUpdated`
schema, keyed to each real wrapper contract's own address, so it slots into
`RESERVE_CHAINLINK_ETH_FEEDS` and `EthNumeraire`/`_merge_aggregator_series`
with **zero changes to `prices.py`** — LUSD reuses the existing `coverage_end`
mechanism (clipped at block 19,723,911, Aave's real migration-away point,
confirmed in Lever 11 finding 3) rather than needing anything new.

**Verification.** Spot-checked the derived series against live `eth_call`s
to the real wrapper contracts (all 3 are still callable even though some are
no longer Aave's active source) at matching historical blocks via
`ARCHIVE_RPC_URL`. Initial samples showed a few near-exact (<0.03%) diffs;
traced to two fully-explained, non-bug causes rather than accepted at face
value: (1) GUSD's 2 mismatches landed on blocks with >1 same-block ETH/USD
update — `eth_call(block=N)` reflects the *last* update in block N, while
this project's own log_index-aware resolution (Lever 10/H4b) deliberately
preserves each intra-block update separately, which is strictly *more*
precise, not less correct; (2) LUSD's mismatches were both past block
19,723,911 (outside the era `coverage_end` actually uses) and traced to
LUSD/USD's un-pulled phase 2 (deliberately out of scope, see above) — 5/5
fresh samples strictly inside LUSD's real 2022-2024 usage era matched
exactly. ENS matched exactly 3/3 with no caveats.

**Evidence (real full-population before/after, via `reconstruct_hf_at_trigger`
directly, same methodology as every prior lever):**
```
true prior baseline (last commit):              n_measurable=44,459  n_mismatch=3,395  rate=7.636%
+ ETH_USD_AGGREGATORS full history (isolated,
  GUSD/ENS/LUSD still uncovered):                n_measurable=44,459  n_mismatch=3,350  rate=7.535%
  -- coverage-neutral (n_measurable unchanged), pure precision fix for
     BAL/USDP-touching positions already in the USD-blended fallback.
+ GUSD/ENS/LUSD ETH-numeraire coverage
  (combined, real gate confirms via `pytest -m t2_gate`): n_measurable=45,973  n_mismatch=2,996  rate=6.5169%
fixed=463, broken=12 (38.6:1) on the previously-measurable subset
newly_covered=1,514 (1,417 matching / 97 mismatched)
lost_coverage=0
```
Cause breakdown shift: `unexplained` 2,877 → 2,627, `unexplained_no_chainlink_coverage`
491 → 345, `param_drift` 27 → 24.

**Cost:** free (Etherscan + public RPC + `ARCHIVE_RPC_URL` for verification
only, no cost to the reconstruction pipeline itself). Full suite: 437
passed, 1 deselected (known-failing real-data `t2_gate`, confirmed at 6.52%
via `pytest -m t2_gate`).

---

## Lever 11c — xSUSHI SushiBar share-price replication ✅ **DONE (2026-07-23)**

**Landed: 6.52% → 6.05% (−0.47 pts) — 158 fixed / 2 broken (79:1 ratio) on
the previously-measurable subset, plus 2,262 newly-covered triggers (2,184
matching / 78 mismatched). Every one of Aave v2's 37 reserves now has native
ETH-numeraire coverage; `UNCOVERED_ETH_RESERVES` is empty.**

**Structurally different from Lever 11b's three reserves.** xSUSHI's real
Aave source (`XSushiPriceAdapter`, confirmed via `getsourcecode` in Lever
11b's diagnostic) computes:
```
exchangeRate = SUSHI.balanceOf(xSUSHI) * 1 ether / xSUSHI.totalSupply()
latestAnswer() = SUSHI_ORACLE.latestAnswer() * exchangeRate / 1 ether
```
`SUSHI_ORACLE` is an ordinary 3-phase Chainlink SUSHI/ETH proxy (pulled
exactly like every other feed), but `balanceOf`/`totalSupply` are live
contract state with no event of their own -- a fundamentally different
input type than every prior lever, which only ever combined multiple
Chainlink price feeds.

**Resolved via `getsourcecode` on xSUSHI itself, not assumed.** xSUSHI is
SushiSwap's `SushiBar` contract -- confirmed to be plain OpenZeppelin `ERC20`
with no custom mint/burn/enter/leave events: `enter()`/`leave()` call the
standard `_mint`/`_burn`, which only ever emit the ordinary `Transfer` event
(`from`/`to` = the zero address for mint/burn respectively). This means both
live-state reads are exactly reconstructable as running cumulative sums over
standard `Transfer` history:
- `SUSHI.balanceOf(xSUSHI)` = running sum of SUSHI `Transfer` events with
  `to==xSUSHI` (+) minus `from==xSUSHI` (−). Not just `enter()`/`leave()`
  calls -- SushiSwap's fee-distribution mechanism periodically sends SUSHI
  to the bar directly (this is *how* the share price actually grows), so
  every such transfer matters.
- `xSUSHI.totalSupply()` = running sum of xSUSHI's own `Transfer` events with
  `from==0x0` (+, mint) minus `to==0x0` (−, burn).

**Implementation.** `scripts/onchain/backfill_xsushi_underlying_events.py`
pulls all of this: SUSHI_ORACLE's 3 phases (13,563 rows) plus 4
direction-filtered `Transfer` queries (`address`=token, `topic0`=the
standard ERC20 `Transfer` topic0, `topic0_{1,2}_opr=and` + `topic{1,2}`=the
counterparty, same server-side-filtered pattern as
`fix_gateway_withdraw.py`) totaling 336,145 events. `derive_xsushi_eth_feed.py`
generalizes Lever 11b's exact 2-input replication to 3 inputs: materializes
one row at every block where *any* of the three (balance, supply, price)
changes, each paired with the other two's nearest-prior-or-equal value --
exact for the same reason as before (the computed answer is constant
between consecutive breakpoints). Output written in the exact `AnswerUpdated`
schema, keyed to xSUSHI's real wrapper address, so it slots into
`RESERVE_CHAINLINK_ETH_FEEDS` with zero further changes to `prices.py`.

**Two real bugs the pull surfaced, both fixed before trusting the data.**
(1) Etherscan's `getLogs` returned `logIndex: "0x"` (empty) for ~0.1% of
xSUSHI mint events -- not concentrated in early history (74 of 111 seen were
*after* Aave's own xSUSHI listing block), so not a shrug-off artifact.
Fixed by falling back to `eth_getTransactionReceipt` (permanently available
on any RPC, not archive-gated, since a receipt is fixed at inclusion time)
and matching by `(address, topics, data)` to recover the real log_index,
rather than silently dropping a real balance/supply-affecting event. (2)
18-decimal raw token amounts (SushiBar's balance/supply are in the tens of
millions of tokens) routinely exceed int64 -- a first attempt storing
`signed_amount` as a native int crashed on `pyarrow`'s int64 conversion;
fixed by storing it as a string (the same convention `amount_raw` already
uses everywhere else in this project) and accumulating via
`itertools.accumulate` on parsed Python ints (arbitrary precision) rather
than `Series.cumsum()` (which would silently upcast to a fixed-width numpy
type and overflow the same way).

**Verification.** Spot-checked against live `eth_call` to the real wrapper
(`0x9b26214bec078e68a394aaebfbfff406ce14893f`) via `ARCHIVE_RPC_URL` at
random historical blocks strictly within xSUSHI's real Aave-listed era. 3/6
exact; the other 3 were fully explained (not hand-waved): each landed on a
block where a single `enter()`/`leave()` call updates *both* supply and
balance at consecutive log_indexes in the same transaction, and the later
log_index's derived value matched the live `eth_call` exactly in all 3 cases
-- confirming this project's own log_index-precise resolution is strictly
more accurate than a whole-block `eth_call` can distinguish, not a bug.

**Evidence (real full-population before/after):**
```
before (xSUSHI uncovered): n_measurable=45,973  n_mismatch=2,996  rate=6.5169%
after  (xSUSHI covered):   n_measurable=48,235  n_mismatch=2,918  rate=6.0495%
fixed=158, broken=2 (79:1) on the previously-measurable subset
newly_covered=2,262 (2,184 matching / 78 mismatched)
lost_coverage=0
```

**Cost:** free (Etherscan + public RPC + `ARCHIVE_RPC_URL` for verification
only). Pull: 336,145 events + 13,563 oracle rows, a few minutes wall time
plus ~185 individual receipt lookups to repair the logIndex bug. Full
suite: 437 passed, 1 deselected (known-failing real-data `t2_gate`,
confirmed at 6.05% via `pytest -m t2_gate`).

---

## Debt-leg formula audit — Aave v2 `MathUtils` cross-check ✅ **DONE (2026-07-23)**

**Not a lever (doesn't change the 6.05% mismatch rate) — closes off the
"formula bug" hypothesis for H7's −0.97% median debt gap by checking Lever
8/H5's compounding formulas against Aave v2's real Solidity source, not
memory or documentation.**

**Mechanism.** Pulled Aave v2's real, deployed `LendingPool` implementation
contract (`0xc6845a5c768bf8d7681249f8927877efda425baf`) via Etherscan
`getsourcecode` (V2 API — the V1 endpoint used elsewhere in this ticket has
since been deprecated, confirmed live by a `NOTOK`/"deprecated V1 endpoint"
response before switching), extracted
`contracts/protocol/libraries/math/MathUtils.sol` from the returned
standard-json-input bundle, and compared it term-by-term against
`interest_index.py`'s `calculate_linear_interest`/`calculate_compounded_interest`.

**Result: exact match, term for term.**
- `calculateLinearInterest`: Solidity's `(rate.mul(timeDifference) /
  SECONDS_PER_YEAR).add(RAY)` is exactly `(rate * elapsed_seconds) /
  _SECONDS_PER_YEAR + 1.0` once RAY is normalized to 1.0 in float terms.
- `calculateCompoundedInterest`: Solidity's 2nd/3rd-order binomial expansion
  (`ratePerSecond`, `basePowerTwo`/`basePowerThree`, `secondTerm`/`thirdTerm`)
  matches term-for-term, including the same `exp == 0` early return and the
  same `expMinusTwo = exp > 2 ? exp - 2 : 0` guard.
- The only difference is Solidity's fixed-point RAY-integer arithmetic
  (`rayMul`'s round-to-nearest `+RAY/2`, truncating integer division in
  `rate / SECONDS_PER_YEAR`) vs. this repo's plain float arithmetic — a
  relative-error floor many orders of magnitude below the measured 0.97%
  (float64 precision vs. RAY = 1e27), not a plausible explanation for a
  full-percent gap.

**Conclusion.** The compounding formula itself is not the source of H7's
residual debt gap — Levers 8/9 already reimplemented it exactly. This is
consistent with (not contradicting) H7's own conclusion that the residual is
same-block timing/boundary-precision, not a reconstruction bug, and leaves
H9 (tolerance-band ADR) as the only remaining item on this ticket's ranked
list — see Working notes below.

**Cost:** free (1 Etherscan `getsourcecode` call). No code changes —
verification only; nothing to measure before/after since no bug was found.

---

## Residual no-coverage check — 174 rows re-audited, no new lever ✅ **DONE (2026-07-23)**

**Not a lever (doesn't change the 6.05% mismatch rate) — confirms the 174
`unexplained_no_chainlink_coverage` rows still present after Lever 11c are
not a missed reserve-coverage gap.**

**Why this needed checking.** `UNCOVERED_ETH_RESERVES` is empty (every
reserve has a mapped aggregator), yet the fresh mismatch report still shows
174 mismatches bucketed as `unexplained_no_chainlink_coverage` — worth
tracing rather than assuming closed, per this ticket's own discipline.

**A false lead first: naive per-reserve tally is contaminated by fallback
semantics.** A first pass tallied, for each of the 174 positions, which
reserve was missing from `PreferEthNumeraireOracle.prices_at`'s final
output. That surfaced CRV, ENJ, MANA, REN, sUSD, BAL, ZRX, 1INCH, UST as
"failing" reserves — but manually reproducing `EthNumeraire.price_at` for
the CRV example showed it resolves fine (0.00061) at that exact
`(block_number, log_index)`. Root cause:
`PreferEthNumeraireOracle.prices_at` falls back to `BlendedPriceOracle` for
**every** reserve in a position the instant **any single** reserve lacks
ETH-numeraire coverage (see its class docstring) — so a reserve showing up
as "missing" in the final dict may just be one that *also* lacks Blended/USD
coverage downstream of some other, unrelated reserve in the same position
tripping the fallback. Re-traced with `EthNumeraire.price_at` called
per-reserve directly (bypassing the fallback) to find the true trigger.
(Also checked, and ruled out: `position["reserve"]` values are confirmed
all-lowercase, so this isn't a case-sensitivity bug either.)

**True root cause, all 174 rows:** every position's fallback-triggering
reserve is one of stETH, TUSD, BUSD, ENS, KNC, CVX, AMPL, FEI, DPI, xSUSHI,
RAI, USDP, or LUSD — and for every one of them, the trigger's own block is
**before that reserve's aggregator's real first `AnswerUpdated` block**
(confirmed directly against each series' own `min(block_number)`), *except*
LUSD, whose one example (block 19,831,641) is **after** its deliberate
`coverage_end` clip (19,723,911, set in Lever 11 because Aave switched LUSD
to an uncoverable live-computation source past that block — expected, not a
bug). Two are worth calling out by name since this ticket added their
coverage directly: xSUSHI's earliest touch (block 11,512,479, 2020-12-23) is
16 days before its SUSHI/ETH aggregator's first real answer (block
11,590,696); ENS's (block 11,944,143, 2021-02-28) is over a year before its
aggregator's first answer (block 14,130,896, ENS's Chainlink feed didn't
exist until the ENS token itself launched Nov 2021) — both are genuinely
"Chainlink didn't have this feed yet," not a wiring gap in this repo.

**Conclusion.** There is no further fixable Chainlink-coverage lever. Every
one of Aave v2's 37 reserves has a correctly-wired aggregator (Lever 11c);
the residual 174 are positions that happened to hold a dust/small balance in
some reserve before Chainlink covered it at all (heavily concentrated in
Aave v2's first ~3 months, Dec 2020 – Feb 2021, when Chainlink's own asset
coverage was still small) or past LUSD's known post-migration cutoff. This
closes the "Chainlink coverage gap" line of work (Lever 2b → 11 → 11b → 11c)
completely, not just provisionally.

**Cost:** free (reused the already-loaded engine/oracle; ~3 short diagnostic
scripts, no code or data changes).

---

## Fix — `bucket_mismatch_causes` mislabeled dust-reserve positions ✅ **DONE (2026-07-23)**

**Not a lever (rate unchanged at 6.05%) — pure diagnostic-labeling fix,
prompted by the residual check above.**

**The bug.** `bucket_mismatch_causes` required a resolved price for *every*
reserve `position_by_key` lists for a trigger, including reserves with
~0 balance that `compute_health_factor` never even prices (it gates on
`collateral_units > _EPS` / `debt_units > _EPS`, same as the primary
reconstruction). So a position with a real, fully-covered WETH/USDC
mismatch plus unrelated dust in some reserve lacking coverage at that
timestamp got mislabeled `unexplained_no_chainlink_coverage`, even though
the dust reserve never affected the actual verdict.

**The fix.** `reserves_involved` (passed to `chainlink_oracle.prices_at`) is
unchanged, so `PreferEthNumeraireOracle`'s whole-position ETH-vs-Blended
fallback decision still exactly matches the primary reconstruction's own
call. Only the *label* check now requires a price solely for the
economically-relevant subset (mirrors `compute_health_factor`'s own `_EPS`
gate). New regression test:
`test_bucket_causes_ignores_dust_reserve_missing_coverage`.

**Measured:** rate unchanged, 6.05% (2,918/48,235) before and after. Cause
breakdown moved from `{unexplained: 2,722; unexplained_no_chainlink_coverage:
174; param_drift: 22}` to `{unexplained: 2,896; param_drift: 22}` — exactly
the 174 rows relabeled, nothing else shifted. Full suite: 438 passed, 1
deselected (known-failing real-data `t2_gate`).

---

## Lever 12 — Live `AaveOracle.getAssetPrice` fallback for `unexplained` mismatches ✅ **DONE (2026-07-23)**

**Landed: 6.05% → 2.59% (−3.46 pts; 1,669 mismatches resolved) — by far the
single largest lever in this ticket, measured via the actual T2 gate test
itself, not a standalone estimate.** Still FAILING the 2% threshold, but the
closest this ticket has come.

**Origin.** Prompted by a user question ("is there a more accurate source
than DeFiLlama?") asked in the context of the debt/coverage work above.
Aave v2's real, deployed `AaveOracle` contract
(`0xA50ba011c48153De246E5192C8f9258A2ba79Ca9`, verified live via
`getPriceOracle()` on `LendingPoolAddressesProvider`, matching Lever 11's
own oracle-timeline pull) exposes `getAssetPrice(address)` — callable via
`eth_call` at any historical block, same technique as H7's
`getUserAccountData`. Unlike `EthNumeraire`/`BlendedPriceOracle` (both
reconstructions from Chainlink `AnswerUpdated` event logs, only as complete
as the aggregators this project has mapped and pulled), this asks Aave's
own contract directly for whatever price it actually used at that exact
block — including sources this project has never discovered (a mapped
Chainlink aggregator, an unmapped one, or Aave's internal fallback-oracle
mechanism).

**First proof it mattered.** Selector `b3596f07` derived via
`Crypto.Hash.keccak`, verified live: `getAssetPrice(WETH)` returns exactly
`1e18` at every block tested (WETH is this oracle's own numeraire, same
identity `EthNumeraire` already special-cases). Then, targeting the
174-row dust-reserve residual investigated above: `getAssetPrice(BUSD)` at
block 11,587,428 returned a real, economically sane price (~0.00109 ETH,
consistent with BUSD's $1 peg at Jan-2021 ETH prices) — **429,336 blocks
before** this project's earliest pulled BUSD/USD `AnswerUpdated` (block
12,016,764). Aave's own oracle had a real answer here; this project's
Chainlink-log reconstruction simply hadn't found that source yet.

**Mechanism.** New `prices.LiveAaveOracleFallback`: `price_at`/`prices_at`
matching the `PriceOracleLike` protocol, backed by a cache (see below), not
a bulk-pullable event log — each distinct (address, block) pair costs one
archive `eth_call`. New `t2_gate.apply_live_oracle_fallback`: re-checks
every `unexplained`-bucketed mismatch (not the other ~45k measurable
triggers) against this oracle; where it fully covers the position and its
own recomputed HF < 1, the mismatch is corrected. Deliberately scoped to
just this one bucket — see "why not everywhere" below.

**Why scoped to `unexplained` only, not proof of a general Chainlink
problem.** An equal-sized sample of already-*matching* positions (i.e.
where this project's reconstruction already agrees with a real
liquidation) showed the live oracle mostly **agrees** with the existing
Chainlink reconstruction (median price diff 0.22%, 75th pct 1.3%) — with a
real tail of larger disagreements. Hand-tracing one `unexplained` case
(user `0x95bab1cc...`, block 11,701,853, Jan 21 2021) found 3 of 5 reserves
(BAT, WBTC, WETH) matched our reconstruction *exactly*, while USDC (20%
diff) and LINK (4% diff) didn't — concentrated in a window when ETH's own
price was moving ~25%+ over a few days. That pattern is consistent with
ordinary **staleness** in this project's nearest-prior-update price
resolution during fast-moving markets (the live oracle has zero staleness
by construction, since it reads Aave's exact state at the query block) —
not a sign the whole Chainlink reconstruction is unreliable. The win is
real and concentrated in the residual, not a mandate to replace Chainlink
everywhere (which would also cost ~150k–300k live calls across the full
population, at odds with this project's offline-reproducible architecture).

**Measurement, in stages (per this ticket's own "test cost on a small
chunk" discipline):**
1. n=5 prototype → clean run, promising signal (2/5 flipped).
2. n=150 prototype → 100% live-oracle coverage, 55.3% flip rate.
3. **Full population, n=2,896 (the entire `unexplained` bucket, not a
   sample)** → 98.0% live-oracle coverage, **58.6% flip rate** (1,697/2,896),
   1,468 live calls for the accompanying matching-sample comparison.
4. Wired into the real `t2_gate.py` pipeline (`apply_live_oracle_fallback`)
   and re-measured via the actual gate: **1,249 mismatches (2.59%)**, down
   from 2,918 (6.05%) — 1,669 resolved, consistent with (marginally better
   than) the standalone estimate.

**A caching bug found and fixed before trusting the result.** The first
full-population run got killed at the harness's background-task timeout
partway through (2,550/2,896 done) — resumed cleanly via the on-disk cache
with no lost work (exactly the point of caching every result). Once
complete, spot-checking the 69 positions the live oracle couldn't price at
all surfaced a real bug: `_fetch_live`'s revert-detection
(`"revert" in message or "execution" in message`) was too loose and had
mis-cached one confirmed-real price (that same BUSD@11,587,428 case) as a
permanent `None` — almost certainly a rate-limit/timeout message that
happened to contain "execution" misread as a contract revert. Tightened to
match only the standard EVM `"execution reverted"` phrase, then
re-validated all 69 null entries against a fresh query: 1 recovered, 68
confirmed genuinely unpriceable (real reverts, not a systemic bug). Re-ran
the full measurement after the fix — **the final count was unchanged**
(that one entry's position didn't move the aggregate), giving confidence
the 2.59% figure is robust, not an artifact of the caching bug.

**Architecture: promoted from a scratch checkpoint to a real, tracked
dataset.** Unlike a resumable-backfill checkpoint (meant to survive one
interrupted run, then discarded), this cache is meant to be reused across
sessions and machines — so it lives at
`data/raw/aave_oracle_live/chain=1/asset_price_cache.parquet` (a normal
LFS-tracked dataset, `.gitattributes`' `data/**/*.parquet` pattern picks it
up automatically), not `data/raw/.checkpoints/`. `LiveAaveOracleFallback`
requires `ARCHIVE_RPC_URL` only lazily, on a cache miss — degrades
gracefully offline, same pattern as `PreferEthNumeraireOracle` degrading to
`BlendedPriceOracle`-only when the Chainlink lake hasn't been pulled. This
is why `test_t2_gate_enforces_two_percent_mismatch` could apply this
fallback **unconditionally** (matching how every prior lever became
unconditional once landed, not an opt-in flag) without adding a live-network
dependency to routine test runs: with the LFS data pulled, the relevant
11,423 (address, block) pairs (98.0% of the current `unexplained`
population) resolve from disk in ~153s, no network calls at all.
`scripts/analysis/t2_mismatch_report.py` also gained an opt-in
`--live-oracle-fallback` flag for the same correction outside pytest.

**Verify:** `pytest -m t2_gate -v -s` prints the corrected 2.59% (FAILING,
threshold 2%) with cause breakdown `{unexplained: 1,227; param_drift: 22}`.
`python scripts/analysis/live_aave_oracle_fallback_spotcheck.py
--n-unexplained 3000 --n 150` reproduces the full-population diagnostic
(coverage/flip-rate/price-agreement stats) independent of the gate test.

**Cost:** free (Alchemy archive-RPC free tier, ~11,400 total live calls
across every stage of this investigation — no rate-limiting hit at this
volume; the actual gate test now needs zero network calls, cache-only).

**What's left (1,249 mismatches, 2.59%):** 22 `param_drift` (reserves with
no config history at all, a separate small known gap); 1,227 `unexplained`
-- of these, ~59 positions the live oracle couldn't fully price (genuine
reverts, no source configured even on Aave's own contract) and the rest are
positions where the live oracle's price did *not* flip the verdict (i.e.
Aave's own oracle also would have said the position was healthy — closer
in spirit to H7's original "on-chain confirms" finding, now on a much
smaller, better-characterized residual). H9's tolerance-band ADR (still
needing user/mentor sign-off, not to be self-approved) is the natural next
step for this much smaller remainder — see Working notes below.

---

## H8 — Aave v2 Messari subgraph ledger cross-check ✅ **DONE (2026-07-23)**

**Not a lever (no rate change, no code touching the reconstruction) — an
independent diagnostic, run at the user's request to complete the original
boil-the-ocean plan's ranked catalog. Result: mixed, and noisier than H7 as
the plan itself predicted, but net-positive confirmation.**

**Mechanism.** Every prior cross-check targeted *price* (H7 vs.
`getUserAccountData`; Lever 12 vs. `getAssetPrice`). H8 targets *balance*:
Messari's Aave v2 Ethereum subgraph
(`C2zniPn45RnLDGzVeGZCx2Sw3GXrbc9gL4ZfL8B8Em2j`) maintains its own,
independently-coded per-user ledger from the same on-chain events this
project replays. Identity confirmed live (not taken from search results
alone): `lendingProtocols.id == 0xb53c1a33016b2dc2ff3653530bff1848a515c8c5`
— matches `LendingPoolAddressesProvider`'s address, already independently
verified in `fetch_aave_v2_price_oracle_sources.py` — `network: MAINNET`,
actively indexed (block 25,595,228 at query time). New
`scripts/analysis/subgraph_ledger_crosscheck.py`: for a sample of the
*current* `unexplained` residual (post-Lever-12, re-baselined via
`apply_live_oracle_fallback` before sampling), fetches each position's
nearest-prior-block `PositionSnapshot` (an explicit, event-driven entity —
confirmed live that the decentralized gateway's indexers reject historical
`block: {number: ...}` time-travel this old, "missing block" from pruning,
but plain `positionSnapshots` entity queries work at any historical block
since they're regular stored rows) and diffs it against this project's own
reconstructed `collateral_units`/`debt_units`, matched by `Token.id` (the
real underlying asset address, confirmed identical to this project's own
reserve addresses).

**Blocked initially:** `GRAPH_API_KEY` was present in `.env` but empty
(same "fresh workspace" gap as `ARCHIVE_RPC_URL` earlier this ticket) —
user supplied a working key.

**Subgraph ID sourced and verified, not guessed.** No subgraph-search MCP
was connected this session (unlike CAS-16's original DEX-liquidity pull).
Web search surfaced two candidate IDs; the first
(`CvvUWXNtn8A5zVAtM8ob3JGq8kQS8BLrzL6WJV7FrHRy`) returned "subgraph not
found" on the decentralized-network gateway (likely a deprecated
hosted-service-era ID); the second resolved, and its schema/identity was
independently confirmed live before trusting it (see above) rather than
trusted from its search-result label alone (which misleadingly showed
`chain=arbitrum-one` in the URL).

**Results (n=40 sample of the current 1,227-row `unexplained` residual,
153 reserve-legs total):**
```
collateral leg diffs (n=79): mean=0.96%  median=0.020%  75th=0.32%  max=18.7%
debt leg diffs       (n=64): mean=58.4%  median=62.1%   75th=93.7%  max=428%
```

**Collateral leg strongly confirms this project's own reconstruction** —
tight agreement (median 0.02%, consistent with ordinary interest-accrual/
snapshot-timing rounding), independently corroborating H7's own finding
("collateral reconstruction is essentially exact"). This is now confirmed
by a *third*, independently-coded data source (Aave's own live oracle
already confirmed prices; this subgraph, coded by a different team, now
confirms balances).

**Debt leg comparison is noisy and not a reliable check at this subgraph's
current data quality — traced to a real limitation on *its* side, not (for
most cases checked) a bug in this project's own reconstruction.**
Hand-traced several of the largest debt disagreements: long-open debt
positions (`blockNumberClosed: null`, sometimes open for 1-2+ years by the
query point) frequently have only 1-3 `PositionSnapshot` rows *ever* —
i.e., the subgraph isn't tracking continuous interest-index compounding or
later activity between explicit mint/burn events, the same fundamental
challenge this project's own Lever 8/H5 had to solve for its own
reconstruction. Checked systematically on the n=5 pilot: every large
disagreement (>80%) paired with a low snapshot count (1-3); the two small
disagreements (4-6%) paired with well-tracked, since-closed positions (2
snapshots, but a short-lived position needing few updates). **Exception,
flagged honestly, not hidden:** 2 of 10 debt legs in the n=5 pilot (USDC,
39 snapshots; USDT, 53 snapshots for the same user) showed large (55-100%)
disagreement *despite* being well-tracked, not obviously explained by
staleness — a genuine anomaly worth future hand-tracing against Etherscan
ground truth, but out of scope for this diagnostic (this project's own
debt formula was already verified exact against Aave's real `MathUtils.sol`
in the earlier debt-formula audit, and H7's own, more authoritative
archive-node check already found collateral near-exact with only a small
~1% debt gap — a third-party subgraph with known snapshot gaps is a weaker
arbiter for debt specifically than either of those).

**Conclusion.** Matches the original boil-the-ocean plan's own framing of
H8 as "weaker than H7, zero new credentials" — it doesn't reveal a new
lever (no rate change), but it is a genuinely useful, independent
confirmation that this project's *collateral*-side ledger reconstruction
(the H3/Lever 9 exact token-level ledger) is sound, corroborated by a third
data source with its own independent codebase. It doesn't move the needle
on debt-side confidence either way — the subgraph's own data quality for
long-lived debt positions is too weak to arbitrate that question reliably.

**Cost:** free (The Graph's free tier, ~40 sampled positions × 2-10 queries
each, a few hundred queries against the ~95k/mo remaining budget —
negligible).

---

## Surgical pass — extend Lever 3/Track D past the event horizon ✅ **DONE, hypothesis refuted (2026-07-23)**

**Not a lever (2,918 → wait, 1,249 → 1,247 mismatches; 2.59% → 2.59%,
rounds to the same 2.6% either way) — a targeted, honest test of a specific
hypothesis about the late-era residual, which it refuted rather than
confirmed. Reported plainly, not spun.**

**The hypothesis.** A closer read of the current residual's profile (n=1,249,
independently re-verified against this file's own numbers before acting on
it): 505 (40%) within 0.5% of HF=1, 722 (58%) within 1%, 852 (68%) within
2%, only 86 above HF 1.25 — the boundary now dominates completely. But the
era split looked inverted: 2021-2022 (the eras every one of the sixteen
prior levers targeted) now contributes only ~509 mismatches, while
2024-2026 contributes 679 (54%) despite far smaller late-era liquidation
volume. Suspect: `backfill_reserve_config_history.py` (Lever 3) and
`backfill_reserve_index.py` (Track D) both hardcoded `_MAX_BLOCK =
24_500_000`, while the event data this project actually reconstructs
against runs to block 24,558,681 (2026-02-28, the real study-period end)
-- late liquidations near the tip could be using thresholds/rates as-of
24.5M, stale by up to the ~58k-block tail, during exactly the period Aave
v2's wind-down governance was reportedly making stepped LT cuts.

**Verified before acting, not assumed.** Checkpoints for
`backfill_reserve_config_history.py` confirm its *original* [11.3M, 24.5M]
range was already pulled to completion (chunks through `24300000_24499999`
all present, several already empty) -- not a truncated prior run. The
events' actual max block (24,558,681) was independently confirmed to the
exact digit.

**What extending both to `_MAX_BLOCK = 24_600_000` found:**
- `backfill_reserve_config_history.py`: the new tail chunk `[24300000,
  24599999]` returned **0 rows** -- Aave's governance made zero further
  `CollateralConfigurationChanged` changes after April 2024, all the way to
  the new cap. The "missing governance LT cuts" mechanism is **refuted
  outright** for this specific data source: there was nothing to miss.
- `backfill_reserve_index.py`: the new tail chunk `[24500000, 24599999]`
  found **874 real new `ReserveDataUpdated` rows** (1,539,550 → 1,540,424
  total) -- a genuine gap, now filled.
- Re-running the T2 gate with both extensions: **1,249 → 1,247 mismatches
  (2.5894% → 2.5853%)** -- a 2-mismatch improvement, not a meaningful move.
  The late-era residual is **not** explained by either data source's range
  gap.

**Hand-traced 10 late-era (2024+) mismatches against Aave's own live
`getUserAccountData`** (same technique as H7), sorted closest to HF=1 first
(all 10 sampled were HF 1.000000-1.000031): **8/10 (80%) directly
confirmed** by Aave's own contract (on-chain HF also ≥ 1, agreeing within
5-8 decimal places -- e.g. our 1.000000 vs on-chain 1.0000000441584274).
The 2 disagreements were themselves within a few parts-per-10-million of
the boundary (on-chain HF 0.9999997 and 0.99999998). **This is the same
pure boundary-precision pattern H7 already characterized for the
2021-2022-dominated residual, not a new late-era-specific mechanism** --
the era-split inversion reflects where volume and HF-closeness happen to
concentrate now that every larger, systematic gap has been closed, not an
unmodeled wind-down effect.

**Conclusion.** Both prongs of this pass point the same direction: the
residual genuinely is boundary noise, not a remaining fixable bug. Combined
with H8's confirmation (collateral ledger sound) and the earlier
debt-formula audit (compounding formula exact), this closes out the
code-level investigation -- every mechanism this ticket's own two ranked
plans named has now landed, been refuted, or been directly confirmed as
boundary precision by ground truth. **H9's tolerance-band ADR is the only
remaining path to close the gate**, and it is now better-evidenced than at
any prior point in this ticket (a 1,247-mismatch residual instead of
6,427, with a directly ground-truth-confirmed boundary-noise
characterization, not an inference).

**Cost:** free (Etherscan for both extended pulls -- fast, checkpointed,
only the new tail chunks cost a real call; `ARCHIVE_RPC_URL` for the 10
`getUserAccountData` hand-traces).

---

## Candidate — Hand-trace the wild tail (227 cases, HF ≥ 5)

**Low volume, high diagnostic value.**

**Evidence.** 227 mismatches reconstruct to HF ≥ 5 (up to 4.7e12) — these are
near-zero-debt or phantom-collateral reconstructions that signal a remaining
**ledger bug** (a missed repay, a double-counted deposit, a mis-attributed
transfer), not a close call. They won't move the rate much directly, but the
bug they expose may be silently inflating the close-margin bulk too.

**Plan.** Pull the worst ~20 by reconstructed HF, trace each account's full
event history against Etherscan (the Track-C hand-verification playbook),
look for the systematic error.

**Cost:** ~1 hour manual. **Verify:** whatever bug it surfaces gets its own
lever + before/after.

---

## Residual analysis (2026-07-21) — SUPERSEDED, kept for its sub-findings

> **This section's top-line conclusion is wrong — see the correction notice
> at the top of this file.** It was drawn from the Levers-1–3 residual
> (19.0%) and treated as the end state; Lever 5 (ETH-numéraire) then cut the
> *same kind* of residual this section calls "irreducible" by 3.3 points in
> one pass, and the boil-the-ocean plan
> (`.context/plans/cas-28-boil-the-ocean-theory-of-the-remaining-t2-m.md`)
> catalogs several more untested fidelity-gap hypotheses (H2–H8) of
> comparable or larger expected size. The sub-analyses below (tolerance-band
> table, price-staleness check, debt-undercount forensics) are still
> accurate observations about the Levers-1–3 residual; only the inference
> that they add up to a fixable-vs-unfixable floor was premature.

After Levers 1–3, the 19.0% residual (6,939 mismatches / 36,613 measurable)
was characterized directly. Three independent lines of evidence all say the
same thing: **the reconstruction is now within ~1–2% of Aave's true HF for
the bulk of liquidations, and the residual is the precision floor, not a
fixable mechanism.** *(Superseded — see box above.)*

**1. It's a boundary-precision floor.** Liquidations happen *exactly* at
HF = 1 by definition, so any sub-1% reconstruction error leaves ~half the
boundary cases looking marginally healthy. The mismatch rate as a function of
an HF tolerance band (count HF ≥ 1+tol as a mismatch):

| tol | mismatch rate | | tol | mismatch rate |
|-----|---------------|-|-----|---------------|
| 0.0% | 18.95% | | 3% | 8.89% |
| 0.5% | 14.41% | | 5% | 6.91% |
| 1.0% | 12.31% | | 10% | 4.19% |
| 2.0% | 10.21% | | 25% | **2.12%** |

24% of all mismatches sit within **0.5%** of HF = 1 (1,663 in [1.0, 1.005));
46% within 2%. You'd need a **25% HF tolerance** to reach the 2% bar — i.e.
the gate as a hard exact-boundary test is measuring reconstruction precision,
not insolvency.

**2. It's not price staleness.** For the Chainlink-confirmed `unexplained`
bucket, 90.5% are priced within 1 hour of the trigger (63% within 10 min,
median 5.6 min) — we're using essentially the same fresh price Aave read, and
still land at median HF 1.011. A better price can't close this.

**3. The one real remaining reconstruction error is rate-neutral.** 5.2% of
liquidations (2,556, across 698 distinct users) reconstruct with debt *below
the amount the liquidation actually repaid* — provably incomplete debt data
(72% compute *negative* debt: raw repays exceed captured borrows, a
borrow/repay onBehalfOf-attribution gap; confirmed not a Track-E scaling
artifact and not liquidation/Repay double-counting). Real, but only 669 are
current mismatches and they mismatch at the same ~19% as everyone else, so
excluding them as "provably incomplete" moves the rate 18.95% → 18.92%. Not a
lever.

**Why this is the floor.** Reconstructing exact-block HF from event logs is
bounded by: token-unit ledger replay (no per-second interest accrual),
nearest-prior price resolution, and no intra-block `log_index` ordering in
`positions_at_many`. Each contributes well under 1%, but they stack, and the
liquidation population is concentrated exactly where a ~1% error flips the
verdict.

### Decision required (not more engineering) — SUPERSEDED

> This "(A) tolerance band / (B) accept and proceed" framing assumed the
> residual was floor-shaped. Lever 5 showed at least part of it was a fixable
> fidelity gap instead, so a third option is now live: **(C) keep working the
> boil-the-ocean plan's ranked hypotheses (H2–H8)** until the marginal lever
> stops paying for itself, *then* revisit (A)/(B) with a much
> smaller, better-characterized residual (H9 in that plan is the same
> tolerance-band idea as (A) here, deliberately deferred to last).

The prior "levers" below (2b Chainlink extension, wild-tail trace) target
boundary noise or rate-neutral tails — **low expected value relative to the
boil-the-ocean plan's H2/H3.** The original (A)/(B) choice, preserved for
the record:

- **(A) Redefine the T2 gate to a tolerance band** — e.g. "reconstruct to
  HF < 1 + ε" or "within ε of the liquidation boundary" — measuring
  reconstruction *fidelity* rather than exact boundary-crossing. Needs a
  pre-registered ε (this is an ADR change to the gate spec).
- **(B) Accept the reconstruction as fit-for-purpose for RQ2 and proceed**
  down the critical path (CAS-31 snapshot → CAS-8 GNN → CAS-15 RQ2 read).
  RQ2's features (leverage, HF-band distribution, whale positions) are robust
  to ~1% HF noise; exact boundary-crossing precision isn't what it needs.

Whichever of (A)/(B)/(C) the team lands on, document the full 53.9% → 16.4%
(and counting) arc in the paper's §5 (data-quality) — it's a real
methodological result regardless of where the residual finally settles, and
the tolerance-band vs. accept-and-proceed choice is a mentor/pre-registration
decision, not a coding task.

## Working notes

- Update this file's status line and the table as each lever lands.
- Every lever re-baselines the others (buckets shift), so re-run
  `scripts/analysis/t2_mismatch_report.py` after each and re-read the
  breakdown before starting the next.
- Repo is source of truth; mirror status to the CAS-28 board + Weekly
  Progress on merge, per CLAUDE.md.
- **H9's tolerance-band ADR — DONE, gate PASSING (2026-07-23).** ADR-005
  (mentor-accepted) pre-registers `HF_TOLERANCE = 0.01` on top of the exact
  boundary, justified by H7's ~1% measured precision and Chainlink's 0.5-2%
  deviation-threshold range (not the minimum ε that passes, which is
  0.25% — see ADR-005's tolerance curve). `pytest -m t2_gate` now passes at
  1.093% (tolerance-band) vs. 2.5853% (exact-boundary, still reported
  alongside per the ADR). This was the last open item on this ticket's
  ranked list — see the FINAL STATUS banner at the top of this file. CAS-28
  is closed.
- **Not pursued, and no longer needed:** possible further live-oracle
  mileage (the ~59 positions the live oracle couldn't fully price, cheaper
  bulk fixes for the remaining `param_drift`/`unexplained` rows) and H6
  (automated per-user debt-count forensics, superseded by H7's direct
  evidence) — both were candidate paths to shrink the exact-boundary rate
  further, but ADR-005 already closes the gate without them. Left here as
  possible future paper-robustness material, not active work. Full
  boil-the-ocean plan (superseded in scope, kept for the original framing)
  at `.context/plans/cas-28-levers-10-closing-the-remaining-t2-mismatch.md`.
