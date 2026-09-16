"""Unit tests + T3 golden-label tests for the D-A cascade labeler (CAS-16/18).

See docs/decisions/ADR-001-cascade-definition.md for the definition and for
the recorded finding that FTX (Nov 2022) does not clear D-A's severity bar on
Aave v2 data — that is asserted here as a documented non-event, not silently
ignored.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cascadesignal.labels.cascade_labeler import (
    GENERATION_LAG_BLOCKS,
    _max_generations,
    _merge_intervals,
    label_dataframe,
    load_liquidations,
)

DATA_DIR = Path("data/raw")


def _has_real_parquet(directory: Path) -> bool:
    """True only if `directory` holds a real parquet file (PAR1 magic bytes),
    not just a Git LFS pointer. Guards the data-dependent tests so they skip
    cleanly when the LFS blobs weren't smudged -- e.g. a CI checkout with
    lfs:false, or a GIT_LFS_SKIP_SMUDGE clone -- instead of crashing while
    trying to read a ~130-byte pointer file as parquet."""
    if not directory.exists():
        return False
    for path in directory.rglob("*.parquet"):
        try:
            with open(path, "rb") as handle:
                if handle.read(4) == b"PAR1":
                    return True
        except OSError:
            continue
    return False


_HAS_REAL_DATA = _has_real_parquet(DATA_DIR / "aave_v2")


# ---------------------------------------------------------------------------
# Unit tests: generation-linking primitive
# ---------------------------------------------------------------------------


def test_max_generations_single_wave_within_lag():
    blocks = np.array([100, 105, 110, 118])
    assets = np.array(["weth"] * 4)
    assert _max_generations(blocks, assets, GENERATION_LAG_BLOCKS) == 1


def test_max_generations_two_waves_across_lag():
    blocks = np.array([100, 105, 200, 205])  # gap of 95 > lag(20) between waves
    assets = np.array(["weth"] * 4)
    assert _max_generations(blocks, assets, GENERATION_LAG_BLOCKS) == 2


def test_max_generations_takes_max_across_assets():
    blocks = np.array([100, 105, 200, 205, 300])
    assets = np.array(["weth", "weth", "wbtc", "wbtc", "wbtc"])
    # weth: 1 wave; wbtc: gap 200->205 within lag (1 wave), then 205->300 > lag (2nd wave) = 2 waves
    assert _max_generations(blocks, assets, GENERATION_LAG_BLOCKS) == 2


def test_max_generations_empty():
    assert _max_generations(np.array([]), np.array([]), GENERATION_LAG_BLOCKS) == 0


# ---------------------------------------------------------------------------
# Unit tests: interval merging
# ---------------------------------------------------------------------------


def test_merge_intervals_overlapping():
    assert _merge_intervals([(1, 10), (5, 15), (20, 30)]) == [(1, 15), (20, 30)]


def test_merge_intervals_empty():
    assert _merge_intervals([]) == []


def test_merge_intervals_touching_boundary():
    assert _merge_intervals([(1, 10), (10, 20)]) == [(1, 20)]


# ---------------------------------------------------------------------------
# Unit test: synthetic end-to-end detection
# ---------------------------------------------------------------------------


def _synthetic_liquidation_df(rows: list[dict]) -> pd.DataFrame:
    base = {
        "chain_id": 1,
        "tx_hash": None,
        "log_index": None,
        "protocol": "aave_v2",
        "event_type": "LiquidationCall",
        "liquidator": "0xliquidator",
        "collateral_seized_raw": None,
        "collateral_seized_usd": None,
    }
    records = []
    for i, r in enumerate(rows):
        rec = dict(base)
        rec.update(r)
        rec.setdefault("tx_hash", f"0xtx{i:04d}")
        rec.setdefault("log_index", i)
        records.append(rec)
    df = pd.DataFrame(records)
    df["block_timestamp"] = pd.to_datetime(df["block_number"] * 12, unit="s", utc=True)
    return df


def test_label_dataframe_detects_synthetic_cascade():
    # Two waves (gap > 20 blocks) on the same collateral asset, 12 distinct
    # accounts, well above a low USD threshold -> should clear a loose grid point.
    rows = []
    block = 1000
    for i in range(6):
        rows.append(
            {
                "block_number": block + i,
                "user": f"0xuser{i}",
                "collateral_asset": "weth",
                "debt_asset": "usdc",
                "amount_raw": "1",
                "amount_usd": 1_000_000.0,
            }
        )
    block2 = block + 100  # > GENERATION_LAG_BLOCKS gap -> second wave/generation
    for i in range(6, 12):
        rows.append(
            {
                "block_number": block2 + (i - 6),
                "user": f"0xuser{i}",
                "collateral_asset": "weth",
                "debt_asset": "usdc",
                "amount_raw": "1",
                "amount_usd": 1_000_000.0,
            }
        )
    df = _synthetic_liquidation_df(rows)
    df = df.sort_values("block_number").reset_index(drop=True)

    episodes = label_dataframe(
        df,
        protocol_tag="synthetic",
        w_grid=(300,),
        k_grid=(5,),
        theta_grid={"p50": 50.0},
    )
    assert len(episodes) == 1
    ep = episodes.iloc[0]
    assert ep["num_positions"] == 12
    assert ep["num_accounts"] == 12
    assert ep["max_generations"] == 2


def test_label_dataframe_rejects_single_account():
    # Many liquidations, but all the same account -> fails the >=2-accounts rule.
    rows = [
        {
            "block_number": 1000 + i,
            "user": "0xsameuser",
            "collateral_asset": "weth",
            "debt_asset": "usdc",
            "amount_raw": "1",
            "amount_usd": 1_000_000.0,
        }
        for i in range(12)
    ]
    df = _synthetic_liquidation_df(rows)
    episodes = label_dataframe(
        df,
        protocol_tag="synthetic",
        w_grid=(300,),
        k_grid=(5,),
        theta_grid={"p50": 50.0},
    )
    assert len(episodes) == 0


def test_label_dataframe_rejects_single_generation():
    # 12 distinct accounts, all within one wave (no gap) -> only 1 generation.
    rows = [
        {
            "block_number": 1000 + i,
            "user": f"0xuser{i}",
            "collateral_asset": "weth",
            "debt_asset": "usdc",
            "amount_raw": "1",
            "amount_usd": 1_000_000.0,
        }
        for i in range(12)
    ]
    df = _synthetic_liquidation_df(rows)
    episodes = label_dataframe(
        df,
        protocol_tag="synthetic",
        w_grid=(300,),
        k_grid=(5,),
        theta_grid={"p50": 50.0},
    )
    assert len(episodes) == 0


# ---------------------------------------------------------------------------
# T3 golden-label tests (CAS-18) — require the ingested Aave v2 data lake.
# ---------------------------------------------------------------------------

CHINA_WINDOW = (
    pd.Timestamp("2021-05-17", tz="UTC"),
    pd.Timestamp("2021-05-21", tz="UTC"),
)
TERRA_WINDOW = (
    pd.Timestamp("2022-05-01", tz="UTC"),
    pd.Timestamp("2022-06-20", tz="UTC"),
)
FTX_WINDOW = (
    pd.Timestamp("2022-11-06", tz="UTC"),
    pd.Timestamp("2022-11-11", tz="UTC"),
)
OCT_2025_WINDOW = (
    pd.Timestamp("2025-10-08", tz="UTC"),
    pd.Timestamp("2025-10-13", tz="UTC"),
)
FEB_2026_WINDOW = (
    pd.Timestamp("2026-01-29", tz="UTC"),
    pd.Timestamp("2026-02-06", tz="UTC"),
)

pytestmark_real_data = pytest.mark.skipif(
    not _HAS_REAL_DATA, reason="requires ingested data/raw/aave_v2 (Git LFS pull)"
)


def _grid_coverage(
    episodes: pd.DataFrame, window: tuple[pd.Timestamp, pd.Timestamp]
) -> int:
    start, end = window
    hits = episodes[(episodes["start_time"] <= end) & (episodes["end_time"] >= start)]
    return hits[["w", "k", "theta_label"]].drop_duplicates().shape[0]


@pytest.fixture(scope="module")
def aave_v2_episodes() -> pd.DataFrame:
    df = load_liquidations(data_dir=DATA_DIR, protocols=("aave_v2",))
    return label_dataframe(df, protocol_tag="aave_v2")


@pytestmark_real_data
def test_china_labels_as_cascade_across_most_of_grid(aave_v2_episodes):
    # 21/27: misses only theta=p99.9 at the two shortest windows (w=50,100) —
    # see ADR-001 Findings. Regression floor, not the full grid.
    assert _grid_coverage(aave_v2_episodes, CHINA_WINDOW) >= 21


@pytestmark_real_data
def test_terra_labels_as_cascade_across_most_of_grid(aave_v2_episodes):
    # Terra's multi-week window straddles several distinct sub-episodes;
    # require broad (not necessarily 27/27) grid coverage. See ADR-001.
    assert _grid_coverage(aave_v2_episodes, TERRA_WINDOW) >= 24


@pytestmark_real_data
def test_ftx_is_reconstructable_but_documented_non_cascade(aave_v2_episodes):
    """FTX (Nov 2022) is a validated non-event under D-A on Aave v2 data —
    see ADR-001 §Findings. This asserts the data is reconstructable (loads,
    non-empty) without asserting it clears the cascade definition."""
    df = load_liquidations(data_dir=DATA_DIR, protocols=("aave_v2",))
    start, end = FTX_WINDOW
    window_events = df[
        (df["block_timestamp"] >= start) & (df["block_timestamp"] <= end)
    ]
    assert (
        len(window_events) > 0
    ), "FTX window must be reconstructable from ingested data"

    coverage = _grid_coverage(aave_v2_episodes, FTX_WINDOW)
    assert coverage == 0, (
        "FTX unexpectedly cleared D-A at some grid point — update ADR-001 §Findings "
        "if this is now a real, verified detection rather than a regression."
    )


@pytest.mark.parametrize("window", [OCT_2025_WINDOW, FEB_2026_WINDOW])
@pytestmark_real_data
def test_oct25_feb26_reconstructable_but_not_detected_on_v2(aave_v2_episodes, window):
    """Oct 2025 and Feb 2026 are reconstructable from Aave v2 data (it loads)
    but do not clear D-A on v2 alone -- v2 activity is negligible by then
    (liquidity moved to v3). This is a scope/data-availability gap, not a
    finding about whether a cascade occurred -- see docs/episodes/."""
    df = load_liquidations(data_dir=DATA_DIR, protocols=("aave_v2",))
    start, end = window
    window_events = df[
        (df["block_timestamp"] >= start) & (df["block_timestamp"] <= end)
    ]
    assert len(window_events) > 0, "window must be reconstructable from ingested data"
    assert _grid_coverage(aave_v2_episodes, window) == 0


@pytestmark_real_data
def test_oct25_detected_on_v3_liquidations():
    """Unlike Feb 2026, Oct 2025 clears D-A once run on Aave v3 mainnet
    liquidations (already ingested; only v3 core events are deferred past
    Mock 1) -- confirms the Oct 2025 gap on v2 is data availability, not a
    real non-event. See docs/episodes/oct-2025.md."""
    df_v3 = load_liquidations(data_dir=DATA_DIR, protocols=("aave_v3",))
    episodes_v3 = label_dataframe(df_v3, protocol_tag="aave_v3")
    assert _grid_coverage(episodes_v3, OCT_2025_WINDOW) > 0
