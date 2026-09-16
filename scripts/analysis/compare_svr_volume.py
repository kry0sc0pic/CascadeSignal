"""Cross-check measured SVR-routed liquidation volume vs Aave's published $675M figure (CAS-47).

Pre-registered regime fact: "Aave integrated Chainlink SVR (OEV
recapture) on Ethereum in Mar 2025 -- $675M liquidations routed through it by
Feb 2026." This script sums `collateral_seized_usd` over Aave v3 mainnet
liquidations this repo labels SVR-routed (`cascadesignal.state.svr.label_svr_routed`)
and compares the total to that figure.

This is a directional cross-check, not an exact reconciliation -- expect our
number to undershoot $675M for reasons that are structural, not bugs:
  - Ethereum-mainnet-only. Aave's figure is very likely cross-chain (SVR
    later expanded to Base/Arbitrum per the governance ARFC "Multi-network
    expansion" thread), and this repo is Ethereum-mainnet-scoped (PLAN §3).
  - Only 6 confirmed Aave-dedicated SVR feeds (AAVE/WBTC/WETH/LINK/USDC/USDT)
    -- assets added in later rollout phases we haven't independently
    confirmed are not included.
  - The same-block-backrun heuristic (see svr.py's module docstring) is a
    detection method, not ground truth -- it can miss genuinely SVR-routed
    liquidations (e.g. if the bundle's price update and liquidation land in
    adjacent blocks under network congestion) but shouldn't produce false
    positives (a same-block SVR proxy update immediately followed by a
    liquidation on that asset is a strong signal either way).

Usage: python scripts/analysis/compare_svr_volume.py
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pandas as pd

from cascadesignal.state.svr import SVR_REGIME_START, label_svr_routed

AAVE_PUBLISHED_SVR_VOLUME_USD = 675_000_000
SVR_EVENTS_DIR = Path("data/raw/chainlink_svr/chain=1")
_STUDY_PERIOD_END = pd.Timestamp("2026-02-28", tz="UTC")


def main() -> None:
    liq_paths = sorted(glob.glob("data/raw/aave_v3/chain=1/*.parquet"))
    if not liq_paths:
        print("No Aave v3 liquidation data found under data/raw/aave_v3/chain=1/.")
        return
    liquidations = pd.concat([pd.read_parquet(p) for p in liq_paths], ignore_index=True)
    liquidations = liquidations[liquidations["event_type"] == "LiquidationCall"].copy()
    liquidations["block_timestamp"] = pd.to_datetime(
        liquidations["block_timestamp"], utc=True
    )

    svr_paths = sorted(glob.glob(str(SVR_EVENTS_DIR / "*.parquet")))
    if not svr_paths:
        print(
            "No SVR proxy AnswerUpdated data found under "
            f"{SVR_EVENTS_DIR} -- scripts/onchain/fetch_svr_feed_events.py "
            "hasn't been run successfully yet (blocked as of 2026-07-15, see "
            "that script's module docstring). Cannot compute a real SVR "
            "volume cross-check without it; skipping rather than reporting "
            "a fabricated $0."
        )
        return

    svr_events = pd.concat([pd.read_parquet(p) for p in svr_paths], ignore_index=True)
    svr_events = svr_events[svr_events["event_type"] == "AnswerUpdated"]

    post_regime = liquidations[liquidations["block_timestamp"] >= SVR_REGIME_START]
    post_regime = post_regime[post_regime["block_timestamp"] <= _STUDY_PERIOD_END]
    print(
        f"{len(post_regime)} Aave v3 mainnet liquidations in "
        f"[{SVR_REGIME_START.date()}, {_STUDY_PERIOD_END.date()}]"
    )

    labeled = label_svr_routed(post_regime, svr_events)
    svr_routed = post_regime[labeled]
    measured_usd = svr_routed["collateral_seized_usd"].fillna(0.0).sum()

    print(f"{len(svr_routed)} liquidations labeled SVR-routed")
    print(f"Measured SVR-routed volume: ${measured_usd:,.0f}")
    print(
        f"Aave's published figure (Ethereum + all networks, by Feb 2026): "
        f"${AAVE_PUBLISHED_SVR_VOLUME_USD:,.0f}"
    )
    if AAVE_PUBLISHED_SVR_VOLUME_USD > 0:
        print(
            f"Measured / published: {measured_usd / AAVE_PUBLISHED_SVR_VOLUME_USD:.1%}"
        )
    print(
        "\nSee this script's module docstring for why undershoot is expected "
        "(mainnet-only scope, 6-feed coverage, detection-heuristic recall)."
    )


if __name__ == "__main__":
    main()
