"""Live AaveOracle.getAssetPrice fallback spot-check for the T2 residual (CAS-28).

`unexplained` is now the dominant mismatch bucket (2,896/2,918 as of the
post-Lever-11c relabeling fix) -- positions where this engine's own
reconstruction *and* an independent re-check with the same Chainlink-backed
oracle both agree HF >= 1, yet a real liquidation happened. Every prior lever
improved the Chainlink *log* reconstruction; this script instead asks Aave
v2's real, deployed `AaveOracle.getAssetPrice(asset)` directly via `eth_call`
on an archive RPC (same technique as `archive_ground_truth_spotcheck.py`'s
H7) -- the actual price Aave itself used at that exact block, regardless of
which underlying source (a mapped Chainlink aggregator, an unmapped one, or
Aave's own internal fallback-oracle mechanism) it came from.

For a sample of `unexplained` mismatches, recomputes each position's HF using
live-oracle prices in place of this engine's own reconstructed
(Chainlink/Blended) prices, and checks whether that flips the verdict to
correctly show HF < 1 -- i.e., whether the residual gap is a *price* problem
(fixable by a better price source) rather than a balance/ledger problem.

Also samples an equal-sized set of already-*matching* (non-mismatched)
positions and compares this engine's reconstructed price against the live
oracle's, reserve by reserve -- if the live oracle systematically agrees with
what this project already reconstructs, that's evidence the existing
Chainlink-log reconstruction is already faithful where it has coverage, and
this fallback's value is concentrated in genuine gaps, not general accuracy.

Usage:
    python scripts/analysis/live_aave_oracle_fallback_spotcheck.py [--n 150] [--seed 42]
Writes experiments/T2/output/live_oracle_fallback_spotcheck.csv.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cascadesignal.state.engine import PositionStateEngine, load_events
from cascadesignal.state.health_factor import compute_health_factor
from cascadesignal.state.prices import LiveAaveOracleFallback, PreferEthNumeraireOracle
from cascadesignal.state.reserve_config_history import ReserveConfigHistory
from cascadesignal.state.reserves import reserve_table
from cascadesignal.state.t2_gate import (
    bucket_mismatch_causes,
    reconstruct_hf_at_trigger,
)

OUT_PATH = Path("experiments/T2/output/live_oracle_fallback_spotcheck.csv")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=150)
    parser.add_argument(
        "--n-unexplained",
        type=int,
        default=None,
        help="overrides --n for the unexplained sample only (e.g. a full-population run)",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    n_unexplained = args.n_unexplained if args.n_unexplained is not None else args.n

    reserves = reserve_table()
    events = load_events(data_dir="data/raw", protocol="aave_v2")
    liquidations = events[events["event_type"] == "LiquidationCall"]
    engine = PositionStateEngine(events=events, reserve_table=reserves)
    price_oracle = PreferEthNumeraireOracle()
    live_oracle = LiveAaveOracleFallback()
    try:
        config_history: ReserveConfigHistory | None = ReserveConfigHistory()
    except FileNotFoundError:
        config_history = None

    report, position_by_key = reconstruct_hf_at_trigger(
        engine, liquidations, price_oracle, config_history
    )
    causes = bucket_mismatch_causes(
        report, position_by_key, engine, price_oracle, config_history
    )

    unexplained = report[causes == "unexplained"]
    matching = report[(~report["mismatch"]) & report["measurable"]]
    print(
        f"{len(unexplained)} unexplained mismatches, {len(matching)} matching positions"
    )

    rng = random.Random(args.seed)
    unexplained_sample = unexplained.loc[
        rng.sample(list(unexplained.index), min(n_unexplained, len(unexplained)))
    ]
    matching_sample = matching.loc[
        rng.sample(list(matching.index), min(args.n, len(matching)))
    ]
    print(
        f"Sampling {len(unexplained_sample)} unexplained + {len(matching_sample)} matching (seed={args.seed})\n"
    )

    def _thresholds_override(block_number: int) -> dict | None:
        return (
            config_history.thresholds_at(block_number)
            if config_history is not None
            else None
        )

    rows = []
    for kind, sample in [
        ("unexplained", unexplained_sample),
        ("matching", matching_sample),
    ]:
        for i, (_idx, row) in enumerate(sample.iterrows(), start=1):
            key = (row["user"], int(row["block_number"]), int(row["log_index"]))
            position = position_by_key[key]
            reserves_involved = position["reserve"].tolist()
            query_block = int(row["block_number"]) - 1

            our_prices = price_oracle.prices_at(
                reserves_involved,
                row["block_timestamp"],
                int(row["block_number"]),
                int(row["log_index"]),
            )
            live_prices = live_oracle.prices_at(
                reserves_involved, row["block_timestamp"], query_block
            )

            # ETH-numeraire-comparable only when our_prices is the raw
            # EthNumeraire leg, not a Blended/USD fallback -- restrict the
            # per-reserve price diff to reserves where both sides are
            # available; a position with zero overlap contributes no rows to
            # the diff distribution below (kept as NaN here, not dropped, so
            # the sample-level counts above stay accurate).
            overlap = [
                r for r in reserves_involved if r in our_prices and r in live_prices
            ]
            diffs_pct = [
                abs(live_prices[r] / our_prices[r] - 1.0)
                for r in overlap
                if our_prices[r] > 0
            ]

            live_covers_all = all(r in live_prices for r in reserves_involved)
            live_result = compute_health_factor(
                position,
                live_prices,
                engine.reserve_table,
                _thresholds_override(int(row["block_number"])),
            )

            rows.append(
                {
                    "kind": kind,
                    "user": row["user"],
                    "block_number": int(row["block_number"]),
                    "log_index": int(row["log_index"]),
                    "our_hf": row["health_factor"],
                    "live_hf": live_result.health_factor,
                    "live_fully_covered": live_result.fully_covered,
                    "live_covers_all_reserves": live_covers_all,
                    "n_reserves": len(reserves_involved),
                    "n_reserves_priced_by_both": len(overlap),
                    "max_price_diff_pct": max(diffs_pct) if diffs_pct else float("nan"),
                    "mean_price_diff_pct": (
                        sum(diffs_pct) / len(diffs_pct) if diffs_pct else float("nan")
                    ),
                    "flipped_to_match": (
                        kind == "unexplained"
                        and live_result.health_factor is not None
                        and live_result.health_factor < 1.0
                    ),
                }
            )
            if i % 25 == 0:
                print(
                    f"  [{kind}] [{i}/{len(sample)}] done, {live_oracle.n_live_calls} live calls so far",
                    flush=True,
                )

    results = pd.DataFrame(rows)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(OUT_PATH, index=False)

    print(f"\nTotal live eth_calls made: {live_oracle.n_live_calls}")

    unexplained_results = results[results["kind"] == "unexplained"]
    n_flipped = int(unexplained_results["flipped_to_match"].sum())
    n_covered = int(unexplained_results["live_covers_all_reserves"].sum())
    print(
        f"\nUnexplained sample (n={len(unexplained_results)}): "
        f"live oracle fully covers {n_covered} ({n_covered / len(unexplained_results):.1%}), "
        f"flips verdict to correctly match {n_flipped} ({n_flipped / len(unexplained_results):.1%})"
    )

    matching_results = results[results["kind"] == "matching"]
    both_priced = matching_results[matching_results["n_reserves_priced_by_both"] > 0]
    print(
        f"\nMatching sample (n={len(matching_results)}): {len(both_priced)} had >=1 reserve "
        "priced by both sources -- price agreement where this project already has coverage:"
    )
    if not both_priced.empty:
        print(both_priced["max_price_diff_pct"].describe().to_string())

    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
