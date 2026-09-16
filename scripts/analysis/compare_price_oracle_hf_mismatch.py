"""Compare DefiLlama-daily vs Chainlink-block-level HF-at-trigger mismatch (CAS-17/CAS-47).

`engine.py`'s module docstring notes real liquidations reconstruct to HF < 1
at trigger for only ~66% of a prior sample (the rest are a mismatch: HF >= 1
at a block where Aave itself executed a liquidation, meaning the reconstructed
state disagrees with reality) -- and that price staleness (DefiLlama daily vs
block-level Chainlink) was flagged as the likely dominant explanation once
`estimate_interest_accrual_gap.py` ruled out interest accrual (consistently
<1% effect, nowhere near 34%).

This script tests that hypothesis directly: for a sample of real liquidations
inside the 5 golden-episode windows, recompute HF-at-trigger twice -- once
with `PriceOracle` (DefiLlama daily), once with `ChainlinkPriceOracle`
(CAS-17, block-timestamp-level) -- restricted to the subset where Chainlink
has *full* price coverage for every reserve in the position (so the
comparison isolates price staleness, not coverage gaps). If Chainlink's
mismatch rate on that same subset is meaningfully lower, block-level pricing
is confirmed as a real driver of the gap.

Usage: python scripts/analysis/compare_price_oracle_hf_mismatch.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pandas as pd

from cascadesignal.state.engine import PositionStateEngine, load_events
from cascadesignal.state.health_factor import compute_health_factor
from cascadesignal.state.prices import ChainlinkPriceOracle, PriceOracle
from cascadesignal.state.reserves import reserve_table

EPISODE_WINDOWS = {
    "China": (12_267_813, 12_767_812),
    "Terra": (14_700_000, 15_000_000),
    "FTX": (15_900_000, 16_000_000),
    "Oct2025": (23_525_000, 23_575_000),
    "Feb2026": (24_330_000, 24_405_000),
}

_SAMPLE_PER_EPISODE = 300
_SEED = 47


def main() -> None:
    reserves = reserve_table()
    defillama = PriceOracle()
    chainlink = ChainlinkPriceOracle()

    events = load_events(data_dir="data/raw", protocol="aave_v2")
    engine = PositionStateEngine(events=events, reserve_table=reserves)
    liquidations = events[events["event_type"] == "LiquidationCall"]

    rows = []
    for episode, (start, end) in EPISODE_WINDOWS.items():
        window = liquidations[
            (liquidations["block_number"] >= start)
            & (liquidations["block_number"] <= end)
        ]
        sample = window.sample(
            n=min(_SAMPLE_PER_EPISODE, len(window)), random_state=_SEED
        )

        for _, liq in sample.iterrows():
            user = liq["user"]
            block = int(liq["block_number"])
            timestamp = liq["block_timestamp"]

            position = engine.account_snapshot(user, block)
            if position.empty:
                continue

            involved = position["reserve"].unique().tolist()
            chainlink_prices = chainlink.prices_at(involved, timestamp)
            chainlink_full_coverage = all(r in chainlink_prices for r in involved)

            defillama_prices = defillama.prices_at(involved, timestamp)
            defillama_result = compute_health_factor(
                position, defillama_prices, reserves
            )

            row = {
                "episode": episode,
                "block": block,
                "defillama_hf": defillama_result.health_factor,
                "defillama_fully_covered": defillama_result.fully_covered,
                "chainlink_full_coverage": chainlink_full_coverage,
            }

            if chainlink_full_coverage:
                chainlink_result = compute_health_factor(
                    position, chainlink_prices, reserves
                )
                row["chainlink_hf"] = chainlink_result.health_factor
                row["chainlink_fully_covered"] = chainlink_result.fully_covered

            rows.append(row)

    out = pd.DataFrame(rows)
    if out.empty:
        print("No liquidations sampled -- check data coverage.")
        return

    print(f"{len(out)} sampled liquidations across {len(EPISODE_WINDOWS)} episodes\n")

    both = out[out["chainlink_full_coverage"] & out["defillama_fully_covered"]].copy()
    both = both.dropna(subset=["defillama_hf", "chainlink_hf"])
    print(
        f"{len(both)} liquidations have BOTH full Chainlink coverage AND full "
        "DefiLlama coverage -- the fair apples-to-apples comparison set.\n"
    )
    if both.empty:
        print("No overlapping-coverage liquidations found -- cannot compare.")
        return

    both["defillama_mismatch"] = both["defillama_hf"] >= 1.0
    both["chainlink_mismatch"] = both["chainlink_hf"] >= 1.0

    summary = both.groupby("episode")[["defillama_mismatch", "chainlink_mismatch"]].agg(
        ["mean", "count"]
    )
    print(summary.to_string())
    print()
    print(
        f"Overall DefiLlama mismatch rate: {both['defillama_mismatch'].mean():.1%} "
        f"(n={len(both)})"
    )
    print(
        f"Overall Chainlink mismatch rate: {both['chainlink_mismatch'].mean():.1%} "
        f"(n={len(both)})"
    )

    coverage_note = out["chainlink_full_coverage"].mean()
    print(
        f"\nChainlink full-position-coverage rate across all {len(out)} sampled "
        f"liquidations (not just the comparison subset): {coverage_note:.1%} -- "
        "the rest have at least one reserve Chainlink doesn't price (see "
        "chainlink_feeds.UNCOVERED_RESERVES) or fall in a gap between the 5 "
        "pulled windows."
    )


if __name__ == "__main__":
    main()
