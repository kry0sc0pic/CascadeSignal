"""Run the D-A cascade labeler across the full definition grid (CAS-16).

Usage:
  uv run python scripts/run_cascade_labeler.py [--config configs/labels.yaml]

Persists data/curated/labels/{protocol_tag}/episodes.parquet (all grid
points) per docs/decisions/ADR-001-cascade-definition.md.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pyarrow.parquet as pq
import yaml

from cascadesignal.labels.cascade_labeler import label_dataframe, load_liquidations
from cascadesignal.labels.schema import to_arrow

log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/labels.yaml"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--out-dir", type=Path, default=Path("data/curated/labels"))
    parser.add_argument(
        "--protocol",
        nargs="+",
        default=None,
        help="Override configs/labels.yaml's protocol list, e.g. --protocol aave_v3.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = yaml.safe_load(args.config.read_text())
    protocols = tuple(args.protocol) if args.protocol else tuple(config["protocols"])
    grid = config["grid"]
    lag = int(config["generation_lag_blocks"])

    protocol_tag = "+".join(protocols)
    log.info("Loading liquidation events for %s from %s", protocol_tag, args.data_dir)
    df = load_liquidations(data_dir=args.data_dir, protocols=protocols)
    log.info(
        "Loaded %d liquidation events (%s -> %s)",
        len(df),
        df["block_timestamp"].min(),
        df["block_timestamp"].max(),
    )

    episodes = label_dataframe(
        df,
        protocol_tag=protocol_tag,
        w_grid=tuple(grid["w"]),
        k_grid=tuple(grid["k"]),
        theta_grid=dict(grid["theta"]),
        lag=lag,
    )
    log.info(
        "Labeled %d cascade episodes across %d grid points",
        len(episodes),
        len(grid["w"]) * len(grid["k"]) * len(grid["theta"]),
    )

    out_dir = args.out_dir / protocol_tag
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "episodes.parquet"
    pq.write_table(to_arrow(episodes), out_path, compression="zstd")
    log.info("Wrote %s", out_path)

    primary = episodes[episodes["is_primary"]]
    summary = {
        "protocol_tag": protocol_tag,
        "definition": "D-A",
        "generation_lag_blocks": lag,
        "total_events": int(len(df)),
        "grid_points": len(grid["w"]) * len(grid["k"]) * len(grid["theta"]),
        "total_episodes": int(len(episodes)),
        "primary_episode_count": int(len(primary)),
        "primary_episodes": [
            {
                "episode_id": row["episode_id"],
                "start_time": str(row["start_time"]),
                "end_time": str(row["end_time"]),
                "total_liquidated_usd": row["total_liquidated_usd"],
                "num_positions": int(row["num_positions"]),
                "num_accounts": int(row["num_accounts"]),
                "max_generations": int(row["max_generations"]),
            }
            for row in primary.to_dict("records")
        ],
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str) + "\n")
    log.info("Wrote %s", summary_path)


if __name__ == "__main__":
    main()
