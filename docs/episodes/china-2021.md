# China crackdown (May 2021)

- **Window analyzed:** 2021-05-01 → 2021-07-01
- **Published scale (PLAN §2, Aave-verified, v2 alone):** $362M / 5,500+ events
- **Observed in our data (Aave v2, same window):** $348.4M / 5,059 liquidation events
  (95.7% of published USD, 92.0% of published count — within T1's 20% reconciliation
  tolerance; see `src/cascadesignal/ingest/integrity.py::CHINA_MAY_JUNE_2021`)

## Mechanism

China's May 2021 mining/trading crackdown triggered a broad crypto sell-off.
ETH and BTC prices dropped sharply over several days (announcement mid-May,
continued deleveraging into June), pushing large numbers of Aave v2
positions below their liquidation threshold. This is the reference
"DeFi-native" cascade in our golden-episode set: the shock originates in
asset prices, not in any single protocol or CeFi counterparty failure.

## Detection status: ✅ Detected

Clears D-A at 21/27 grid points (the primary point and all but the
strictest severity percentile at the two shortest windows — see ADR-001
§Findings for the exact grid breakdown). Peak intensity: $77.8M liquidated
in a single 100-block window on 2021-05-19, 172 positions, 155 accounts,
5 generations.

## Data coverage

Aave v2 mainnet liquidations, complete for this window (T1: no block-coverage
gaps, dedup rate within known bound). No mempool coverage (pre-Aug-2023).
