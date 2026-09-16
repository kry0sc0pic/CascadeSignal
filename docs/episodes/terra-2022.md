# Terra/LUNA collapse + stETH depeg (May–Jun 2022)

- **Window analyzed:** 2022-05-01 → 2022-06-20
- **Published scale (PLAN §2, Aave-verified, platform-wide):** 32,000+ positions
  liquidated over a week. No single platform-wide USD figure is published for
  this episode; not directly comparable position-count-vs-event-count, so no
  strict reconciliation tolerance is asserted here (see T1 for why only the
  China target gets one).
- **Observed in our data (Aave v2, same window):** $251.4M / 10,064 liquidation
  events

## Mechanism

Two linked shocks six weeks apart: the Terra/LUNA collapse (~May 9–12, 2022)
and the stETH depeg / Celsius-insolvency panic (mid-June 2022). Both
transmitted into Aave v2 through falling collateral prices (stETH's
depeg directly hit stETH-collateralized positions). The window is wide
enough to capture both legs, which is why D-A resolves this as several
distinct sub-episodes rather than one continuous event — the primary grid
finds two separate cascades in this window: one around 2021-12-04 (a
distinct earlier deleveraging cluster, not part of Terra) is excluded here;
the two Terra-window cascades are 2022-05 (LUNA) and 2022-06-14 (stETH
depeg / Celsius panic), $49.4M peak, 222 positions, 214 accounts, 5
generations for the latter.

## Detection status: ✅ Detected

Clears D-A at 24/27 grid points.

## Data coverage

Aave v2 mainnet liquidations, complete for this window. No mempool coverage
(pre-Aug-2023).
