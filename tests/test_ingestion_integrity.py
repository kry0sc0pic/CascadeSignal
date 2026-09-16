"""T1 ingestion-integrity tests (CAS-12): dedup, USD sanity, block-coverage
continuity, and reconciliation against Aave's published liquidation stats.

See src/cascadesignal/ingest/integrity.py for why only the China (May-Jun
2021) reconciliation target is asserted with a tolerance -- the Feb 2026 and
lifetime published figures are platform-wide, not comparable against our
Aave-v2-mainnet-only data at Mock 1 scope.
"""

from __future__ import annotations

import glob
from pathlib import Path

import pandas as pd
import pytest

from cascadesignal.ingest.gap_fill import audit_coverage
from cascadesignal.ingest.integrity import (
    CHINA_MAY_JUNE_2021,
    FEB_2026_RECORD,
    LIFETIME,
    check_usd_sanity,
    count_duplicate_events,
    reconcile,
)
from cascadesignal.labels.cascade_labeler import load_liquidations

DATA_DIR = Path("data/raw")
PROTOCOLS = ("aave_v2", "aave_v3", "compound_v2", "compound_v3", "maker")


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

# Aave v2/v3 have a completed USD price join (CAS-5); Compound v2/v3 and Maker
# do not (out of Mock 1 scope -- MVP = Aave v2+v3). These are canaries, not
# permissive thresholds: they fail loudly the day someone *does* wire up a
# price join for these protocols, as a reminder to move them into the
# strict-sanity group and delete the corresponding canary.
USD_JOINED_PROTOCOLS = ("aave_v2", "aave_v3")
USD_UNJOINED_PROTOCOLS = ("compound_v2", "compound_v3", "maker")

# A handful of exact-duplicate rows exist at the boundary between an
# original chunked ingest and a later gap-fill re-pull that slightly
# overlapped an already-covered range (found via this test). Downstream
# consumers must dedupe on (tx_hash, log_index, user) before use --
# cascade_labeler.load_liquidations already does. This tolerance is a ceiling
# on that known artifact, not a license for new duplication.
MAX_DUPLICATE_RATE = 0.0001

pytestmark_real_data = pytest.mark.skipif(
    not _HAS_REAL_DATA, reason="requires ingested data/raw (Git LFS pull)"
)


def _load_all_events(protocol: str) -> pd.DataFrame:
    paths = sorted(glob.glob(str(DATA_DIR / protocol / "chain=*" / "*.parquet")))
    return pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)


@pytest.fixture(scope="module")
def aave_v2_liquidations() -> pd.DataFrame:
    return load_liquidations(data_dir=DATA_DIR, protocols=("aave_v2",))


@pytestmark_real_data
@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_duplicate_rate_within_known_bound(protocol):
    df = _load_all_events(protocol)
    rate = count_duplicate_events(df) / len(df)
    assert rate <= MAX_DUPLICATE_RATE, (
        f"{protocol} duplicate rate {rate:.4%} exceeds the known chunk-boundary "
        f"artifact bound ({MAX_DUPLICATE_RATE:.4%}) -- investigate as a new regression."
    )


@pytestmark_real_data
@pytest.mark.parametrize("protocol", USD_JOINED_PROTOCOLS)
def test_usd_sanity_priced_protocols(protocol):
    df = _load_all_events(protocol)
    result = check_usd_sanity(df)
    assert result["negative_usd"] == 0
    assert result["non_finite_usd"] == 0
    assert result["above_ceiling"] == 0
    # A handful of nulls is expected (events without a resolvable USD price);
    # a large fraction would indicate a broken price join.
    assert result["null_usd"] / len(df) < 0.01


@pytestmark_real_data
@pytest.mark.parametrize("protocol", USD_UNJOINED_PROTOCOLS)
def test_usd_join_gap_is_known_and_unchanged(protocol):
    df = _load_all_events(protocol)
    result = check_usd_sanity(df)
    assert result["null_usd"] == len(df), (
        f"{protocol} now has USD-priced events -- move it into "
        "USD_JOINED_PROTOCOLS and assert real sanity bounds instead."
    )


@pytestmark_real_data
@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_block_coverage_has_no_gaps(protocol):
    coverage = audit_coverage(DATA_DIR)
    key = f"{protocol}/chain=1"
    assert key in coverage, f"no ingested data found for {key}"
    assert (
        coverage[key]["gaps"] == []
    ), f"{key} has uncovered block ranges: {coverage[key]['gaps']}"


@pytestmark_real_data
def test_china_reconciles_within_tolerance(aave_v2_liquidations):
    result = reconcile(aave_v2_liquidations, CHINA_MAY_JUNE_2021)
    assert result.within_tolerance, (
        f"China (May-Jun 2021) reconciliation out of tolerance: "
        f"observed {result.observed_events} events / ${result.observed_usd:,.0f} vs "
        f"published {CHINA_MAY_JUNE_2021.published_events} / ${CHINA_MAY_JUNE_2021.published_usd:,.0f} "
        f"(event_dev={result.event_deviation:+.1%}, usd_dev={result.usd_deviation:+.1%})"
    )


@pytestmark_real_data
@pytest.mark.parametrize("target", [FEB_2026_RECORD, LIFETIME])
def test_platform_wide_targets_reported_not_asserted(aave_v2_liquidations, target):
    """Feb 2026 and lifetime are platform-wide figures; v2-only data is
    expected to fall well short. Assert the comparison is explicitly marked
    non-applicable rather than silently passing or failing a tolerance."""
    result = reconcile(aave_v2_liquidations, target)
    assert target.applicable is False
    assert result.within_tolerance is False
    assert result.observed_events >= 0  # sanity: the window loads without error
