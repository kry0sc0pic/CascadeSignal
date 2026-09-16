"""Full-period contagion-graph snapshot materialization (CAS-31, MVP-13).

Runs `graph.build.build_snapshots` over the whole Aave v2 study period and
persists the result via `graph.io.save_snapshots`, so a downstream consumer
can load a precomputed snapshot sequence instead of replaying state
reconstruction on every run.

**GATED ON T2.** The CAS-31 ticket's acceptance criterion is explicit:
materialization is blocked until T2 (state-reconstruction gate) passes. T2
closed via ADR-005 (2026-07-23): the gate's operative number is now the
tolerance-band rate (`HF >= 1.0 + HF_TOLERANCE`, ε=1%, `state.t2_gate.
HF_TOLERANCE`), corrected by Lever 12's live-oracle fallback
(`apply_live_oracle_fallback`) -- both applied here exactly as
`scripts/analysis/t2_mismatch_report.py` does, not just the raw
reconstruction (an earlier version of this script measured only the
uncorrected rate and reported a false FAILING). This script re-checks the
live rate before doing any real work and refuses to run the full
materialization unless it's at or under `MISMATCH_THRESHOLD`, or `--force`
is passed. `--force` is an explicit "I know this artifact is provisional"
override for smoke-testing the pipeline itself -- not a way to sneak a
tainted artifact into `data/`.

**CADENCE.** CAS-31 specifies snapshots every 25 blocks
(5 in stress windows). Over the full study period (~13.2M blocks) that's
~530k anchors -- at ~0.7s/snapshot (measured on real data, mid-sized
snapshot ~700 nodes / ~3,400 edges) that's over four days of wall time, far
beyond what proved tractable for the identical problem in this project's
earlier (now-removed) feature-bar pipeline, which settled on a ~7,200-block
cadence (~1 day). This script defaults to the literal PLAN cadence but
always prints a measured runtime
estimate before committing (and refuses to proceed past that estimate
without `--yes`), so the actual cadence is a conscious, measured choice, the
same "measure before wiring" discipline CAS-28's levers used throughout --
not a silent substitution of a different number than the ticket specifies.

Usage:
    python scripts/graph/materialize_snapshots.py --dry-run
    python scripts/graph/materialize_snapshots.py \\
        --normal-cadence-blocks 7200 --stress-cadence-blocks 1440 --yes
"""

from __future__ import annotations

import argparse
import gc
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from cascadesignal.graph.build import (
    DEFAULT_SNAPSHOT_CADENCE_BLOCKS,
    DEFAULT_TOP_N_WHALES,
    STRESS_SNAPSHOT_CADENCE_BLOCKS,
    build_snapshots,
)
from cascadesignal.graph.io import EDGES_FILENAME, NODES_FILENAME, save_snapshots
from cascadesignal.graph.pool_depth import load_pool_depth_bars
from cascadesignal.graph.stress_windows import (
    detect_stress_windows,
    snapshot_blocks_with_stress,
)
from cascadesignal.state.engine import PositionStateEngine, load_events
from cascadesignal.state.prices import LiveAaveOracleFallback, PreferEthNumeraireOracle
from cascadesignal.state.reserve_config_history import ReserveConfigHistory
from cascadesignal.state.reserves import reserve_table
from cascadesignal.state.t2_gate import (
    HF_TOLERANCE,
    MISMATCH_THRESHOLD,
    apply_live_oracle_fallback,
    bucket_mismatch_causes,
    mismatch_summary,
    reconstruct_hf_at_trigger,
)

DEFAULT_OUT_DIR = Path("data/curated/graph/aave_v2")
BARS_PATH = Path("data/features/aave_v2/feature_bars_v0.parquet")
_BENCHMARK_SAMPLE_SIZE = 5
DEFAULT_BATCH_SIZE = 500


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--start-block", type=int, default=None)
    parser.add_argument("--end-block", type=int, default=None)
    parser.add_argument(
        "--normal-cadence-blocks", type=int, default=DEFAULT_SNAPSHOT_CADENCE_BLOCKS
    )
    parser.add_argument(
        "--stress-cadence-blocks", type=int, default=STRESS_SNAPSHOT_CADENCE_BLOCKS
    )
    parser.add_argument("--top-n-whales", type=int, default=DEFAULT_TOP_N_WHALES)
    parser.add_argument("--limit-snapshots", type=int, default=None)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Snapshots per in-memory batch before flushing to disk and "
        "freeing memory -- bounds peak RSS regardless of total snapshot "
        "count. The correct stress-cadence fix densifies real cascades "
        "enough that holding every snapshot in memory at once (the "
        "previous single-shot approach) OOMs past a few thousand snapshots.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the snapshot plan and a measured runtime estimate; write nothing.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the runtime-estimate confirmation prompt for a non-dry-run.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if the live T2 gate is failing. Produces a provisional "
        "artifact -- see module docstring.",
    )
    return parser.parse_args()


def _nearest_prior_timestamp(events: pd.DataFrame, blocks: np.ndarray) -> pd.Series:
    """Causal nearest-prior-block timestamp lookup: each block's timestamp
    is the latest observed event at or before it."""
    ev = events.sort_values("block_number", kind="mergesort")
    ev_blocks = ev["block_number"].to_numpy(dtype=np.int64)
    ev_ts = ev["block_timestamp"].to_numpy()
    idx = np.searchsorted(ev_blocks, blocks, side="right") - 1
    idx = np.clip(idx, 0, len(ev_ts) - 1)
    return pd.to_datetime(pd.Series(ev_ts[idx]), utc=True).reset_index(drop=True)


def _check_t2_gate(
    engine: PositionStateEngine, price_oracle, config_history
) -> tuple[float, float]:
    """Live T2 mismatch rate (re-measured, not cached) -- mirrors
    `scripts/analysis/t2_mismatch_report.py`'s full corrected pipeline
    (bucket causes -> Lever 12 live-oracle fallback -> summary), not just
    the raw reconstruction, since Lever 12's correction is what actually
    gets the rate under the gate (ADR-005). Returns
    `(mismatch_rate, mismatch_rate_exact)` -- the tolerance-band rate (the
    gate's operative number, `HF >= 1.0 + HF_TOLERANCE`) and the
    exact-boundary rate (`HF >= 1.0`), per ADR-005's commitment to never
    report one without the other.
    """
    liquidations = engine.events[engine.events["event_type"] == "LiquidationCall"]
    report, position_by_key = reconstruct_hf_at_trigger(
        engine, liquidations, price_oracle, config_history
    )
    causes = bucket_mismatch_causes(
        report, position_by_key, engine, price_oracle, config_history
    )
    live_oracle = LiveAaveOracleFallback()
    report = apply_live_oracle_fallback(
        report, causes, position_by_key, engine, live_oracle, config_history
    )
    summary = mismatch_summary(report)
    mismatch_rate = float(summary.loc["OVERALL", "mismatch_rate"])  # type: ignore[arg-type]
    mismatch_rate_exact = float(summary.loc["OVERALL", "mismatch_rate_exact"])  # type: ignore[arg-type]
    return mismatch_rate, mismatch_rate_exact


def _merge_batches(batch_dir: Path, out_dir: Path, n_batches: int) -> None:
    """Concatenate each batch's `nodes.parquet`/`edges.parquet` into the final
    `out_dir` files, one row-group per batch via `pq.ParquetWriter` -- so the
    merge itself never holds more than one batch's table in memory, matching
    the whole point of batching the build in the first place."""
    for filename in (NODES_FILENAME, EDGES_FILENAME):
        writer = None
        try:
            for i in range(n_batches):
                table = pq.read_table(batch_dir / f"batch_{i:05d}" / filename)
                if writer is None:
                    writer = pq.ParquetWriter(out_dir / filename, table.schema)
                writer.write_table(table)
        finally:
            if writer is not None:
                writer.close()


def main() -> None:
    args = _parse_args()

    reserves = reserve_table()
    events = load_events(data_dir="data/raw", protocol="aave_v2")
    engine = PositionStateEngine(events=events, reserve_table=reserves)
    price_oracle = PreferEthNumeraireOracle()
    try:
        config_history: ReserveConfigHistory | None = ReserveConfigHistory()
    except FileNotFoundError:
        config_history = None

    print("Checking live T2 state-reconstruction gate...")
    mismatch_rate, mismatch_rate_exact = _check_t2_gate(
        engine, price_oracle, config_history
    )
    gate_passing = mismatch_rate <= MISMATCH_THRESHOLD
    print(
        f"  ADR-005 tolerance-band rate (HF_TOLERANCE={HF_TOLERANCE:.0%}, "
        f"the gate's operative number): {mismatch_rate:.4%} "
        f"(threshold {MISMATCH_THRESHOLD:.0%}) -- "
        f"{'PASSING' if gate_passing else 'FAILING'}; "
        f"exact-boundary rate (HF >= 1.0): {mismatch_rate_exact:.4%}"
    )
    if not gate_passing and not args.force:
        print(
            "\nREFUSING to materialize: CAS-31's acceptance criterion is "
            "explicitly blocked until T2 passes -- materializing now would "
            "bake in state already known to be wrong (see this script's "
            "module docstring and graph/build.py's SCOPE NOTE). "
            "Re-run once T2 passes, or pass --force to build a provisional "
            "artifact anyway."
        )
        sys.exit(1)
    if not gate_passing and args.force:
        print(
            "  --force set: proceeding despite a failing T2 gate. This "
            "artifact is PROVISIONAL -- do not treat it as CAS-31's "
            "production materialization."
        )

    observed = events["block_number"].to_numpy(dtype=np.int64)
    start_block = int(observed.min()) if args.start_block is None else args.start_block
    end_block = int(observed.max()) if args.end_block is None else args.end_block

    if BARS_PATH.exists():
        bars = pd.read_parquet(BARS_PATH)
        stress_windows = detect_stress_windows(bars)
        print(
            f"Detected {len(stress_windows)} stress window(s) from {BARS_PATH} "
            "(note: this bars file may predate the launch-month backfill and "
            "the latest CAS-28 state-reconstruction levers -- stress "
            "detection degrades gracefully to normal cadence where stale, "
            "it doesn't corrupt snapshot content)."
        )
    else:
        print(
            f"{BARS_PATH} not found -- no stress-window densification, uniform cadence."
        )
        stress_windows = []

    blocks = snapshot_blocks_with_stress(
        start_block,
        end_block,
        stress_windows,
        normal_cadence=args.normal_cadence_blocks,
        stress_cadence=args.stress_cadence_blocks,
    )
    if args.limit_snapshots is not None:
        blocks = blocks[: args.limit_snapshots]
    if len(blocks) == 0:
        print("No snapshot anchors in range -- nothing to do.")
        return

    print(
        f"Plan: {len(blocks)} snapshot(s) over blocks [{start_block}, {end_block}] "
        f"(normal cadence {args.normal_cadence_blocks}, "
        f"stress cadence {args.stress_cadence_blocks})"
    )

    pool_bars = load_pool_depth_bars()
    timestamps = _nearest_prior_timestamp(events, blocks)

    sample_idx = np.linspace(
        0, len(blocks) - 1, num=min(_BENCHMARK_SAMPLE_SIZE, len(blocks)), dtype=int
    )
    sample_idx = np.unique(sample_idx)
    t0 = time.time()
    build_snapshots(
        engine,
        price_oracle,
        blocks[sample_idx],
        timestamps.iloc[sample_idx].reset_index(drop=True),
        top_n_whales=args.top_n_whales,
        pool_depth_bars=pool_bars,
    )
    per_snapshot_s = (time.time() - t0) / len(sample_idx)
    est_total_s = per_snapshot_s * len(blocks)
    print(
        f"Measured {per_snapshot_s:.2f}s/snapshot on a {len(sample_idx)}-snapshot "
        f"sample -> estimated total {est_total_s / 60:.1f} min for all "
        f"{len(blocks)} snapshots."
    )

    if args.dry_run:
        print("--dry-run: stopping before the full build.")
        return
    if not args.yes:
        response = input(f"Proceed with the full {len(blocks)}-snapshot build? [y/N] ")
        if response.strip().lower() != "y":
            print("Aborted.")
            return

    n_batches = -(-len(blocks) // args.batch_size)  # ceil division
    print(
        f"Building full snapshot sequence in {n_batches} batch(es) of up to "
        f"{args.batch_size} snapshots each (bounds peak memory)..."
    )
    batch_dir = args.out_dir / ".batches"
    if batch_dir.exists():
        shutil.rmtree(batch_dir)
    t0 = time.time()
    n_nodes = 0
    n_edges = 0
    for i in range(n_batches):
        lo = i * args.batch_size
        hi = min(lo + args.batch_size, len(blocks))
        batch_snapshots = build_snapshots(
            engine,
            price_oracle,
            blocks[lo:hi],
            timestamps.iloc[lo:hi].reset_index(drop=True),
            top_n_whales=args.top_n_whales,
            pool_depth_bars=pool_bars,
        )
        n_nodes += sum(len(s.nodes) for s in batch_snapshots)
        n_edges += sum(len(s.edges) for s in batch_snapshots)
        save_snapshots(batch_snapshots, batch_dir / f"batch_{i:05d}")
        del batch_snapshots
        gc.collect()
        print(
            f"  batch {i + 1}/{n_batches}: blocks[{lo}:{hi}] done "
            f"({(time.time() - t0) / 60:.1f} min elapsed)"
        )
    print(f"Built {len(blocks)} snapshots in {(time.time() - t0) / 60:.1f} min.")

    print("Merging batches into final output...")
    _merge_batches(batch_dir, args.out_dir, n_batches)
    shutil.rmtree(batch_dir)
    print(
        f"Wrote {n_nodes} node rows / {n_edges} edge rows across "
        f"{len(blocks)} snapshots to {args.out_dir}/"
    )


if __name__ == "__main__":
    main()
