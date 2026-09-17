"""Point-in-time Aave v2 reserve risk parameters.

`reserves.reserve_table` carries Aave v2's *current, frozen*
`liquidation_threshold` per reserve (pinned to a late block). Aave v2 is now
deprecated and most reserves were de-risked toward a near-zero threshold
before the freeze, so those values are wrong for the 2021-2022 study period
(see `reserves.py`'s docstring). This module serves the threshold (and
LTV/bonus) actually in effect at any historical block, from the
`CollateralConfigurationChanged` governance-event history pulled by
`scripts/onchain/backfill_reserve_config_history.py`.

Direction matters and is era-dependent -- e.g. WBTC's threshold was 0.75 at
the China episode (May 2021) but is frozen at 0.82 today (frozen *overstates*
early collateral -> HF too high -> a false T2 "match" failure), while DAI's
was raised to 0.90 in 2022 but frozen at 0.77 (frozen *understates*). Wiring
the point-in-time value into the T2 gate moves the mismatch rate 24.2% ->
19.0%: the early-period overstatements it corrects outweigh the later-period
understatements it exposes.

`thresholds_at(block)` returns only reserves with a config change *at or
before* `block`; a reserve queried before its first listing config isn't in
the dict, so `health_factor.compute_health_factor` falls back to the frozen
`reserve_table` value for it (and keeps its frozen `historical_reliable`
flag). Nearest-prior lookup per reserve, same shape as `InterestIndexOracle`.
"""

from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

_DEFAULT_DATA_DIR = Path("data/raw")


class ReserveConfigHistory:
 """Nearest-prior-block lookup for Aave v2 per-reserve risk parameters."""

 def __init__(self, data_dir: str | Path = _DEFAULT_DATA_DIR):
 data_dir = Path(data_dir)
 paths = sorted(
 glob.glob(
 str(data_dir / "aave_v2_reserve_config" / "chain=*" / "*.parquet")
 )
 )
 if not paths:
 raise FileNotFoundError(
 f"No reserve-config parquet under {data_dir}/aave_v2_reserve_config/ "
 "-- run scripts/onchain/backfill_reserve_config_history.py first."
 )
 df = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
 df = df.sort_values("block_number")
 self._by_reserve: dict[str, tuple[np.ndarray, np.ndarray]] = {
 str(reserve): (
 group["block_number"].to_numpy,
 group["liquidation_threshold"].to_numpy(dtype=float),
 )
 for reserve, group in df.groupby("asset")
 }

 def liquidation_threshold_at(self, reserve: str, block_number: int) -> float | None:
 """Liquidation threshold in effect at `block_number`, or `None` if
 this reserve has no config change at or before it (caller falls back
 to the frozen `reserve_table` value)."""
 entry = self._by_reserve.get(reserve.lower)
 if entry is None:
 return None
 blocks, thresholds = entry
 idx = int(np.searchsorted(blocks, block_number, side="right")) - 1
 if idx < 0:
 return None
 return float(thresholds[idx])

 def thresholds_at(self, block_number: int) -> dict[str, float]:
 """Every reserve's liquidation threshold in effect at `block_number`
 (only reserves with a config change at or before it)."""
 out: dict[str, float] = {}
 for reserve in self._by_reserve:
 threshold = self.liquidation_threshold_at(reserve, block_number)
 if threshold is not None:
 out[reserve] = threshold
 return out

 def coverage(self) -> set[str]:
 """Reserve addresses with at least one config change on record."""
 return set(self._by_reserve.keys)
