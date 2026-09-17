"""Full T2 mismatch diagnostics report.

`tests/test_t2_mismatch_gate.py::test_t2_gate_enforces_two_percent_mismatch`
is the actual CI/local enforcement (fails the suite if > 2% of measurable
real Aave v2 liquidations reconstruct to HF >= 1 at trigger) -- it prints a
summary inline on failure, but doesn't persist a file, to avoid a `pytest`
run silently writing a multi-MB artifact into the repo tree.

This script is the human-run counterpart: it reproduces the same
reconstruction over the full liquidation population and writes the
per-protocol + overall mismatch-rate summary, plus a per-event diagnostics
table (tx, asset, block, cause) restricted to the mismatched liquidations
themselves -- the actionable subset, not the full ~49k population -- to
`experiments/T2/output/`.

Usage: python scripts/analysis/t2_mismatch_report.py [--live-oracle-fallback]

`--live-oracle-fallback` additionally re-checks every `unexplained`
mismatch against Aave v2's real, live `AaveOracle.getAssetPrice`
(`prices.LiveAaveOracleFallback`) and corrects `mismatch` wherever it fully
covers the position and confirms HF < 1 -- see
`t2_gate.apply_live_oracle_fallback`'s docstring for what this measures and
why it's scoped to just that one bucket, not the full population.

Prints both the tolerance-band rate (the gate's operative number, `HF >=
1.0 + HF_TOLERANCE`) and the exact-boundary rate (`HF >= 1.0`), per ADR-005's
commitment to never report one without the other.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cascadesignal.state.engine import PositionStateEngine, load_events
from cascadesignal.state.prices import LiveAaveOracleFallback, PreferEthNumeraireOracle
from cascadesignal.state.reserve_config_history import ReserveConfigHistory
from cascadesignal.state.reserves import reserve_table
from cascadesignal.state.t2_gate import (
 HF_TOLERANCE,
 MISMATCH_THRESHOLD,
 PriceOracleLike,
 apply_live_oracle_fallback,
 attach_diagnostics,
 bucket_mismatch_causes,
 mismatch_summary,
 reconstruct_hf_at_trigger,
)

OUT_DIR = Path(__file__).parent.parent.parent / "experiments" / "T2" / "output"


def main -> None:
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument("--live-oracle-fallback", action="store_true")
 args = parser.parse_args

 reserves = reserve_table
 events = load_events(data_dir="data/raw", protocol="aave_v2")
 liquidations = events[events["event_type"] == "LiquidationCall"]
 engine = PositionStateEngine(events=events, reserve_table=reserves)
 # Primary oracle: native asset/ETH pricing per position where
 # every leg has ETH coverage (the numeraire Aave v2 itself reads),
 # falling back to Chainlink-blended USD (Lever 2) for the whole position
 # otherwise -- see prices.PreferEthNumeraireOracle.
 price_oracle: PriceOracleLike = PreferEthNumeraireOracle

 # Point-in-time liquidation thresholds: use the value in
 # effect at each trigger block, not the frozen/de-risked current one.
 try:
 config_history: ReserveConfigHistory | None = ReserveConfigHistory
 except FileNotFoundError:
 config_history = None

 report, position_by_key = reconstruct_hf_at_trigger(
 engine, liquidations, price_oracle, config_history
 )

 #: bucket against `price_oracle` itself, not a
 # separate, narrower asset/USD `ChainlinkPriceOracle`. That standalone
 # oracle was never extended by H2's full-history ETH-feed pull (a
 # completely different aggregator map, `RESERVE_CHAINLINK_FEEDS` vs
 # `RESERVE_CHAINLINK_ETH_FEEDS`), so it was still golden-windows-only
 # checking coverage against it wildly overstated
 # `unexplained_no_chainlink_coverage` (3,413 vs the true 547 once
 # checked against the oracle actually used for the primary
 # reconstruction) and made `oracle_lag` look larger than it really was.
 # See `bucket_mismatch_causes`'s docstring for what each bucket means
 # now that the primary oracle already *is* the best available Chainlink
 # price (`oracle_lag` is expected to land at ~0, not removed outright in
 # case a genuinely independent, better price source is wired in later).
 causes = bucket_mismatch_causes(
 report, position_by_key, engine, price_oracle, config_history
 )

 if args.live_oracle_fallback:
 live_oracle = LiveAaveOracleFallback
 report = apply_live_oracle_fallback(
 report, causes, position_by_key, engine, live_oracle, config_history
 )
 # Corrected rows are no longer mismatches, so they no longer belong
 # in any mismatch-cause bucket either.
 causes = causes.where(report["mismatch"])

 summary = mismatch_summary(report)
 diagnostics = attach_diagnostics(report, liquidations, causes)
 mismatched_diagnostics = diagnostics[diagnostics["mismatch"].fillna(False)]

 OUT_DIR.mkdir(parents=True, exist_ok=True)
 summary.to_csv(OUT_DIR / "mismatch_summary.csv")
 mismatched_diagnostics.to_csv(OUT_DIR / "mismatch_diagnostics.csv", index=False)

 print(
 f"{len(liquidations)} total liquidations, {len(report)} unique trigger keys\n"
 )
 print("Per-protocol + overall mismatch rate (restricted to measurable triggers):")
 print(summary.to_string)

 overall_rate = float(summary.loc["OVERALL", "mismatch_rate"]) # type: ignore[arg-type]
 overall_rate_exact = float(summary.loc["OVERALL", "mismatch_rate_exact"]) # type: ignore[arg-type]
 print(
 f"\nT2 gate threshold: {MISMATCH_THRESHOLD:.0%} -- "
 f"{'PASS' if overall_rate <= MISMATCH_THRESHOLD else 'FAIL'} -- "
 f"ADR-005 tolerance-band rate (HF_TOLERANCE={HF_TOLERANCE:.0%}): "
 f"{overall_rate:.4%}; exact-boundary rate (HF >= 1.0): "
 f"{overall_rate_exact:.4%}"
 )

 print(
 f"\nMismatch cause breakdown ({len(mismatched_diagnostics)} mismatched events):"
 )
 print(mismatched_diagnostics["cause"].value_counts.to_string)
 print(f"\nWrote {OUT_DIR}/mismatch_summary.csv and mismatch_diagnostics.csv")


if __name__ == "__main__":
 main
