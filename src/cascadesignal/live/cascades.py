"""Live "cascade" boundary: a UI-side liquidation-clustering heuristic.

Deliberately NOT the ADR-001 D-A cascade definition
(`labels/cascade_labeler.py`): D-A requires `amount_usd` severity +
position/account breadth + per-asset generation-linking, none of which are
available live (`live/decode.py` sets `amount_usd = None` for every live
row). This module only does the one thing that's actually computable from
the live confirmed-liquidation stream: group liquidations that occurred
close together in block-time, so the UI has something to click into. It
carries no severity/breadth qualification and computes no hit/miss/accuracy
judgment about the algorithm's alarm -- that's for the human reviewing the
UI to decide, not this code.

The default gap value (20 blocks) matches ADR-001's `GENERATION_LAG_BLOCKS`
for continuity of intuition ("~4 minutes of propagation time"), but is
defined independently here -- not imported from `cascade_labeler.py` -- so
retuning this heuristic never touches, or is mistaken for, the pre-registered
offline definition.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DEFAULT_CLUSTER_GAP_BLOCKS = 20
# 5, not 2: at a minimum of 2 roughly half of all clusters are 2-liquidation
# blips nobody would call a cascade, and including them drags measured
# early-warning coverage from ~60% down to ~15% -- they swamp the real events.
DEFAULT_MIN_CLUSTER_SIZE = 5


@dataclass
class ClusterState:
    """The currently-open (in-progress) cluster, carried across calls."""

    start_block: int | None = None
    last_block: int | None = None
    count: int = 0

    def is_open(self) -> bool:
        return self.start_block is not None


@dataclass
class ClosedCluster:
    start_block: int
    end_block: int
    n_liquidations: int


def update_cluster(
    state: ClusterState,
    new_blocks: np.ndarray,
    gap_blocks: int = DEFAULT_CLUSTER_GAP_BLOCKS,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
) -> tuple[ClusterState, list[ClosedCluster]]:
    """Fold newly-confirmed liquidation block numbers into the open cluster,
    closing (and emitting) it whenever a gap > `gap_blocks` is crossed.

    `new_blocks` must be sorted ascending (the caller's confirmed-liquidation
    feed already is). Returns the updated state and zero or more closed
    clusters that reached `min_cluster_size` -- smaller gaps are dropped
    silently, not surfaced as noise. A cluster still open at the end of
    `new_blocks` stays open in the returned state for the next call.
    """
    closed: list[ClosedCluster] = []
    for block in np.sort(new_blocks).tolist():
        block = int(block)
        if state.is_open() and block - state.last_block > gap_blocks:
            if state.count >= min_cluster_size:
                closed.append(
                    ClosedCluster(
                        start_block=state.start_block,
                        end_block=state.last_block,
                        n_liquidations=state.count,
                    )
                )
            state = ClusterState()
        if not state.is_open():
            state = ClusterState(start_block=block, last_block=block, count=1)
        else:
            state.last_block = block
            state.count += 1
    return state, closed


__all__ = [
    "DEFAULT_CLUSTER_GAP_BLOCKS",
    "DEFAULT_MIN_CLUSTER_SIZE",
    "ClusterState",
    "ClosedCluster",
    "update_cluster",
]
