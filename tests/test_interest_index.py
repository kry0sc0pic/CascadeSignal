"""Tests for the Aave v2 interest-index oracle.

Coverage is real pulled data only -- there's no meaningful synthetic fixture
to substitute, so these tests skip cleanly (mirroring
tests/test_cascade_labeler.py's guard) when the LFS-tracked
`data/raw/aave_v2_reserve_index/` isn't present. Since D's
full-range Etherscan pull (`scripts/onchain/backfill_reserve_index.py`),
WETH's coverage is one continuous [11.5M, 24.5M] window rather than 5
disjoint golden-episode windows.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from cascadesignal.state.interest_index import (
 InterestIndexOracle,
 calculate_compounded_interest,
 calculate_linear_interest,
)

DATA_DIR = Path("data/raw")
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
_RAY = 10**27


# ---------------------------------------------------------------------------
# calculate_linear_interest / calculate_compounded_interest
# pure functions, no fixture needed.
# ---------------------------------------------------------------------------


def test_linear_interest_zero_elapsed_is_identity:
 assert calculate_linear_interest(0.05, 0) == pytest.approx(1.0)


def test_linear_interest_matches_simple_interest_at_one_year:
 # 5% APR, exactly one year elapsed -> exactly 1.05x (linear by definition).
 assert calculate_linear_interest(0.05, 365 * 86400) == pytest.approx(1.05)


def test_compounded_interest_zero_elapsed_is_identity:
 assert calculate_compounded_interest(0.05, 0) == pytest.approx(1.0)


def test_compounded_interest_exceeds_linear_at_one_year:
 # The binomial expansion's 2nd/3rd-order terms make compounding strictly
 # greater than simple linear growth over a full year.
 linear = calculate_linear_interest(0.05, 365 * 86400)
 compounded = calculate_compounded_interest(0.05, 365 * 86400)
 assert compounded > linear
 # Close to true continuous compounding (e^0.05 ~= 1.05127) -- the 2nd/3rd
 # order approximation should track it closely at this rate/duration.
 assert compounded == pytest.approx(1.05127, abs=1e-4)


# ---------------------------------------------------------------------------
# InterestIndexOracle timestamp-based compounding, synthetic
# fixture -- always runs, unlike the real-data tests below.
# ---------------------------------------------------------------------------


@pytest.fixture
def synthetic_oracle(tmp_path: Path) -> InterestIndexOracle:
 out_dir = tmp_path / "aave_v2_reserve_index" / "chain=1"
 out_dir.mkdir(parents=True)
 row = {
 "chain_id": 1,
 "block_number": 100,
 "block_timestamp": pd.Timestamp("2022-01-01", tz="UTC"),
 "reserve": WETH,
 "liquidity_index_raw": str(int(1.0 * _RAY)),
 "variable_borrow_index_raw": str(int(1.0 * _RAY)),
 "liquidity_rate_raw": str(int(0.05 * _RAY)),
 "variable_borrow_rate_raw": str(int(0.05 * _RAY)),
 }
 pd.DataFrame([row]).to_parquet(out_dir / "fake.parquet")
 return InterestIndexOracle(data_dir=tmp_path)


def test_index_at_without_timestamp_returns_stored_value(synthetic_oracle):
 assert synthetic_oracle.index_at(WETH, 200, "liquidity") == pytest.approx(1.0)


def test_index_at_with_timestamp_compounds_forward(synthetic_oracle):
 one_year_later = pd.Timestamp("2023-01-01", tz="UTC")
 liquidity = synthetic_oracle.index_at(
 WETH, 200, "liquidity", query_timestamp=one_year_later
 )
 variable = synthetic_oracle.index_at(
 WETH, 200, "variable_borrow", query_timestamp=one_year_later
 )
 assert liquidity == pytest.approx(1.05, abs=1e-3) # linear, 5% APR
 assert variable > liquidity # compounded > linear at the same rate/duration


def test_index_at_many_with_query_timestamps_matches_index_at(synthetic_oracle):
 one_year_later = pd.Timestamp("2023-01-01", tz="UTC")
 single = synthetic_oracle.index_at(
 WETH, 200, "variable_borrow", query_timestamp=one_year_later
 )
 batch = synthetic_oracle.index_at_many(
 pd.Series([WETH]),
 pd.Series([200]),
 "variable_borrow",
 query_timestamps=pd.Series([one_year_later]),
 )
 assert batch.iloc[0] == pytest.approx(single)


def test_index_at_many_without_query_timestamps_unchanged(synthetic_oracle):
 batch = synthetic_oracle.index_at_many(
 pd.Series([WETH]), pd.Series([200]), "liquidity"
 )
 assert batch.iloc[0] == pytest.approx(1.0)


def _has_real_parquet(directory: Path) -> bool:
 if not directory.exists:
 return False
 for path in directory.rglob("*.parquet"):
 try:
 with open(path, "rb") as handle:
 if handle.read(4) == b"PAR1":
 return True
 except OSError:
 continue
 return False


pytestmark = pytest.mark.skipif(
 not _has_real_parquet(DATA_DIR / "aave_v2_reserve_index"),
 reason="requires ingested data/raw/aave_v2_reserve_index (Git LFS pull)",
)


@pytest.fixture(scope="module")
def oracle -> InterestIndexOracle:
 return InterestIndexOracle(data_dir=DATA_DIR)


def test_coverage_returns_a_single_continuous_window(oracle):
 windows = oracle.coverage[WETH]
 # Track D's full-range Etherscan pull closed the gaps between
 # the 5 golden-episode windows Dune had pulled, so WETH now has one
 # contiguous run spanning the whole pulled block range.
 assert len(windows) == 1
 start, end = windows[0]
 assert start < 12_000_000 # at/near China's episode start
 assert end > 24_000_000 # near Track D's pull ceiling


def test_index_at_grows_within_the_window(oracle):
 start, end = oracle.coverage[WETH][0]
 idx_start = oracle.index_at(WETH, start, "liquidity")
 idx_end = oracle.index_at(WETH, end, "liquidity")
 assert idx_start is not None and idx_end is not None
 assert idx_end >= idx_start # liquidityIndex is monotonically non-decreasing


def test_index_at_returns_none_outside_the_covered_range(oracle):
 start, end = oracle.coverage[WETH][0]
 # Blocks well before/after the pulled range must not silently return
 # the nearest edge value -- the _MAX_GAP_BLOCKS guard should reject them.
 assert oracle.index_at(WETH, start - 1_000_000, "liquidity") is None
 assert oracle.index_at(WETH, end + 1_000_000, "liquidity") is None


def test_index_at_unknown_reserve_returns_none(oracle):
 assert (
 oracle.index_at("0xnotareserve00000000000000000000000000000", 12_300_000)
 is None
 )


def test_rate_at_returns_a_small_fraction(oracle):
 china_start = oracle.coverage[WETH][0][0]
 rate = oracle.rate_at(WETH, china_start, "liquidity")
 assert rate is not None
 # Aave v2 supply APRs were never anywhere near 100%/block-instant terms.
 assert 0.0 <= rate < 1.0
