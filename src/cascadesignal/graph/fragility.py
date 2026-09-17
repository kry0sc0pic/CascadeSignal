"""Systemic-fragility covariate from the contagion-graph snapshots.

Reduces each materialized `whale_position` snapshot to one scalar -- the share
of tracked positions sitting in the danger band just above the liquidation
line -- for use as the Hawkes baseline covariate (models/hawkes.py's optional
`cov_col`). Rationale: the Hawkes background rate `mu` is the *exogenous*
liquidation arrival rate (liquidations not triggered by a prior one), and what
drives that is the stock of collateral near its liquidation threshold -- a
price dip liquidates more of it at once when more positions sit just above the
line. So the fraction of positions in `[HF_LO, HF_HI)` is the natural covariate
for `mu`, and it beat raw USD totals (which just track the ETH price level) and
other bands on a causal next-bar correlation check (see the 2026-08 graph-
baseline pass).

Only Aave v2 has a materialized graph (data/curated/graph/aave_v2), so this is
the one protocol the graph baseline runs on.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# Danger band: above the HF=1 liquidation line but close to it. Positions with
# HF far below 1 that persist are dust (uneconomical to liquidate), not signal;
# positions well above 1.25 are comfortably collateralized.
AT_RISK_HF_LO = 1.0
AT_RISK_HF_HI = 1.25

COV_COL = "frac_at_risk"


def build_fragility_covariate(
 nodes_path: str | Path,
 *,
 hf_lo: float = AT_RISK_HF_LO,
 hf_hi: float = AT_RISK_HF_HI,
) -> pd.DataFrame:
 """One `frac_at_risk` scalar per graph snapshot, ascending by block.

 Args:
 nodes_path: a `graph.io`-materialized `nodes.parquet`.
 hf_lo, hf_hi: the danger band `[hf_lo, hf_hi)` on `health_factor`.

 Returns:
 Columns `snapshot_block`, `snapshot_timestamp`, `frac_at_risk` -- the
 share of that snapshot's `whale_position` nodes whose health factor is
 in the band (denominator is all tracked positions, so a position with
 no debt / undefined HF counts as not-at-risk). As-of joinable to the
 liquidation bars by `snapshot_block` (see run_hawkes_eval.py).
 """
 nodes = pd.read_parquet(
 nodes_path,
 columns=["node_type", "snapshot_block", "snapshot_timestamp", "health_factor"],
 )
 pos = nodes[nodes["node_type"] == "whale_position"]
 hf = pos["health_factor"]
 in_band = ((hf >= hf_lo) & (hf < hf_hi)).astype(float)
 cov = (
 in_band.groupby([pos["snapshot_block"], pos["snapshot_timestamp"]])
 .mean
 .rename(COV_COL)
 .reset_index
 .sort_values("snapshot_block")
 .reset_index(drop=True)
 )
 return cov


__all__ = ["build_fragility_covariate", "AT_RISK_HF_LO", "AT_RISK_HF_HI", "COV_COL"]
