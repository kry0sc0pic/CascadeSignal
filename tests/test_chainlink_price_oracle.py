"""Unit tests for `ChainlinkPriceOracle` (CAS-17/CAS-47), `BlendedPriceOracle`
(CAS-28 Lever 2), and `EthNumeraire`/`PreferEthNumeraireOracle` (CAS-28 H1)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import cascadesignal.state.prices as prices_module
from cascadesignal.state.prices import (
    BlendedPriceOracle,
    ChainlinkPriceOracle,
    EthNumeraire,
    PreferEthNumeraireOracle,
)

RESERVE_A = "0x000000000000000000000000000000000000a1"
RESERVE_B_ETH_QUOTED = "0x000000000000000000000000000000000000b2"
UNCOVERED_RESERVE = "0x000000000000000000000000000000000000c3"

USD_AGGREGATOR = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
USD_AGGREGATOR_V2 = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"  # feed migration
ETH_QUOTED_AGGREGATOR = "0xcccccccccccccccccccccccccccccccccccccc"
ETH_USD_AGGREGATOR = "0xdddddddddddddddddddddddddddddddddddddd"


def _row(
    user: str,
    timestamp: str,
    raw: int,
    event_type: str = "AnswerUpdated",
    block_number: int = 0,
    log_index: int = 0,
) -> dict:
    return {
        "user": user,
        "block_number": block_number,
        "log_index": log_index,
        "block_timestamp": pd.Timestamp(timestamp, tz="UTC"),
        "amount_raw": str(raw),
        "event_type": event_type,
    }


@pytest.fixture
def chainlink_dir(tmp_path: Path) -> Path:
    rows = [
        # RESERVE_A: USD-quoted, decimals=8, split across two aggregators
        # (a feed migration) to test series-merging. block_number order
        # matches timestamp order throughout (see `_merge_aggregator_series`'s
        # docstring -- block_number order is a strict refinement).
        _row(USD_AGGREGATOR, "2022-05-10T00:00:00", 200_00000000, block_number=100),
        _row(USD_AGGREGATOR, "2022-05-11T00:00:00", 190_00000000, block_number=200),
        _row(USD_AGGREGATOR_V2, "2022-05-12T00:00:00", 150_00000000, block_number=300),
        # ETH/USD, decimals=8 -- needed to convert RESERVE_B's ETH quote.
        _row(
            ETH_USD_AGGREGATOR, "2022-05-10T00:00:00", 2000_00000000, block_number=100
        ),
        _row(
            ETH_USD_AGGREGATOR, "2022-05-12T00:00:00", 1800_00000000, block_number=300
        ),
        # RESERVE_B: ETH-quoted, decimals=18 (0.01 ETH = 1e16 raw).
        _row(ETH_QUOTED_AGGREGATOR, "2022-05-10T00:00:00", 10**16, block_number=100),
        # A non-AnswerUpdated row that must be filtered out.
        _row(
            USD_AGGREGATOR,
            "2022-05-13T00:00:00",
            999_00000000,
            event_type="Other",
            block_number=400,
        ),
    ]
    df = pd.DataFrame(rows)
    path = tmp_path / "chainlink.parquet"
    df.to_parquet(path)
    return tmp_path


@pytest.fixture
def feed_map() -> dict:
    return {
        RESERVE_A: {
            "quote": "USD",
            "decimals": 8,
            "aggregators": [USD_AGGREGATOR, USD_AGGREGATOR_V2],
        },
        RESERVE_B_ETH_QUOTED: {
            "quote": "ETH",
            "decimals": 18,
            "aggregators": [ETH_QUOTED_AGGREGATOR],
        },
    }


def _oracle(chainlink_dir: Path, feed_map: dict) -> ChainlinkPriceOracle:
    return ChainlinkPriceOracle(
        chainlink_dir=chainlink_dir,
        feed_map=feed_map,
        eth_usd_aggregators=[ETH_USD_AGGREGATOR],
    )


def test_coverage_reflects_feed_map(chainlink_dir, feed_map):
    oracle = _oracle(chainlink_dir, feed_map)
    assert oracle.coverage() == {RESERVE_A, RESERVE_B_ETH_QUOTED}


def test_nearest_prior_lookup_across_migrated_aggregators(chainlink_dir, feed_map):
    oracle = _oracle(chainlink_dir, feed_map)
    # Between the 05-11 (old aggregator) and 05-12 (new aggregator) points --
    # nearest prior should be the 05-11 value from the OLD aggregator,
    # proving the two addresses were merged into one series.
    price = oracle.price_at(RESERVE_A, pd.Timestamp("2022-05-11T12:00:00", tz="UTC"))
    assert price == pytest.approx(190.0)

    price_after_migration = oracle.price_at(
        RESERVE_A, pd.Timestamp("2022-05-12T06:00:00", tz="UTC")
    )
    assert price_after_migration == pytest.approx(150.0)


def test_eth_quoted_reserve_converted_to_usd(chainlink_dir, feed_map):
    oracle = _oracle(chainlink_dir, feed_map)
    # RESERVE_B is 0.01 ETH at 05-10, ETH/USD is 2000 at 05-10 -> $20.
    price = oracle.price_at(
        RESERVE_B_ETH_QUOTED, pd.Timestamp("2022-05-10T12:00:00", tz="UTC")
    )
    assert price == pytest.approx(20.0)


def test_returns_none_for_uncovered_reserve(chainlink_dir, feed_map):
    oracle = _oracle(chainlink_dir, feed_map)
    assert oracle.price_at(UNCOVERED_RESERVE, pd.Timestamp.now(tz="UTC")) is None


def test_returns_none_when_no_prior_data_exists(chainlink_dir, feed_map):
    oracle = _oracle(chainlink_dir, feed_map)
    before_any_data = pd.Timestamp("2022-01-01T00:00:00", tz="UTC")
    assert oracle.price_at(RESERVE_A, before_any_data) is None


def test_returns_none_when_gap_exceeds_max_staleness(chainlink_dir, feed_map):
    oracle = ChainlinkPriceOracle(
        chainlink_dir=chainlink_dir,
        feed_map=feed_map,
        eth_usd_aggregators=[ETH_USD_AGGREGATOR],
        max_staleness=pd.Timedelta(hours=6),
    )
    # Nearest prior point for RESERVE_A is 05-12T00:00 -- 36h before this
    # timestamp, well past the 6h staleness budget.
    far_timestamp = pd.Timestamp("2022-05-13T12:00:00", tz="UTC")
    assert oracle.price_at(RESERVE_A, far_timestamp) is None


def test_non_answerupdated_events_are_ignored(chainlink_dir, feed_map):
    oracle = _oracle(chainlink_dir, feed_map)
    # The "Other"-typed 05-13 row (raw 999) must never surface.
    price = oracle.price_at(RESERVE_A, pd.Timestamp("2022-05-14T00:00:00", tz="UTC"))
    assert price != pytest.approx(999.0)


def test_prices_at_batches_multiple_addresses(chainlink_dir, feed_map):
    oracle = _oracle(chainlink_dir, feed_map)
    result = oracle.prices_at(
        [RESERVE_A, RESERVE_B_ETH_QUOTED, UNCOVERED_RESERVE],
        pd.Timestamp("2022-05-10T12:00:00", tz="UTC"),
    )
    assert set(result.keys()) == {RESERVE_A, RESERVE_B_ETH_QUOTED}
    assert result[RESERVE_A] == pytest.approx(200.0)
    assert result[RESERVE_B_ETH_QUOTED] == pytest.approx(20.0)


def test_prices_at_many_matches_price_at_loop(chainlink_dir, feed_map):
    oracle = _oracle(chainlink_dir, feed_map)
    timestamps = pd.to_datetime(
        [
            "2022-05-09T00:00:00",  # before any data -> None
            "2022-05-10T12:00:00",  # first point
            "2022-05-11T12:00:00",  # after migration-eligible point
            "2022-05-12T06:00:00",  # after migration
        ],
        utc=True,
    )
    expected = [oracle.price_at(RESERVE_A, ts) for ts in timestamps]
    got = oracle.prices_at_many(RESERVE_A, pd.Series(timestamps))
    for e, g in zip(expected, got):
        if e is None:
            assert np.isnan(g)
        else:
            assert g == pytest.approx(e)


def test_prices_at_many_respects_max_staleness(chainlink_dir, feed_map):
    oracle = ChainlinkPriceOracle(
        chainlink_dir=chainlink_dir,
        feed_map=feed_map,
        eth_usd_aggregators=[ETH_USD_AGGREGATOR],
        max_staleness=pd.Timedelta(hours=6),
    )
    far_timestamp = pd.Timestamp("2022-05-13T12:00:00", tz="UTC")
    got = oracle.prices_at_many(RESERVE_A, pd.Series([far_timestamp]))
    assert np.isnan(got[0])


def test_prices_at_many_uncovered_reserve_returns_all_nan(chainlink_dir, feed_map):
    oracle = _oracle(chainlink_dir, feed_map)
    got = oracle.prices_at_many(
        UNCOVERED_RESERVE, pd.Series([pd.Timestamp.now(tz="UTC")] * 5)
    )
    assert np.isnan(got).all()


def test_missing_chainlink_dir_raises():
    with pytest.raises(FileNotFoundError):
        ChainlinkPriceOracle(chainlink_dir="data/raw/does_not_exist_dir")


# ---------------------------------------------------------------------------
# Same-block log_index ordering (CAS-28 H4b)
# ---------------------------------------------------------------------------


@pytest.fixture
def same_block_dir(tmp_path: Path) -> Path:
    rows = [
        # Two updates within the SAME block (200) -- identical
        # block_timestamp, different log_index. Timestamp-only resolution
        # can't distinguish their order; (block_number, log_index) can.
        _row(
            USD_AGGREGATOR,
            "2022-06-01T00:00:00",
            100_00000000,
            block_number=200,
            log_index=5,
        ),
        _row(
            USD_AGGREGATOR,
            "2022-06-01T00:00:00",
            300_00000000,
            block_number=200,
            log_index=15,
        ),
    ]
    df = pd.DataFrame(rows)
    path = tmp_path / "chainlink.parquet"
    df.to_parquet(path)
    return tmp_path


def _same_block_oracle(same_block_dir: Path) -> ChainlinkPriceOracle:
    feed_map = {
        RESERVE_A: {"quote": "USD", "decimals": 8, "aggregators": [USD_AGGREGATOR]}
    }
    return ChainlinkPriceOracle(
        chainlink_dir=same_block_dir, feed_map=feed_map, eth_usd_aggregators=[]
    )


def test_price_at_log_index_ordering_sees_earlier_same_block_update(same_block_dir):
    oracle = _same_block_oracle(same_block_dir)
    ts = pd.Timestamp("2022-06-01T00:00:00", tz="UTC")

    # Query log_index 10 sits between the two updates (5, 15) -- must see
    # the FIRST (100.0), not the second (300.0), even though both share
    # block_timestamp.
    price = oracle.price_at(RESERVE_A, ts, block_number=200, log_index=10)
    assert price == pytest.approx(100.0)


def test_price_at_log_index_ordering_excludes_same_block_update_after_query(
    same_block_dir,
):
    oracle = _same_block_oracle(same_block_dir)
    ts = pd.Timestamp("2022-06-01T00:00:00", tz="UTC")

    # Query log_index 3 is strictly before BOTH updates (5, 15) -- neither
    # is visible; no prior data at all -> None.
    price = oracle.price_at(RESERVE_A, ts, block_number=200, log_index=3)
    assert price is None


def test_price_at_without_log_index_sees_last_same_block_update(same_block_dir):
    # Pre-H4b behavior, unchanged when block_number/log_index are omitted:
    # timestamp-only resolution can't distinguish intra-block order, so it
    # sees the LAST update in the block -- the exact look-ahead gap H4b's
    # log_index-aware resolution closes when the query's own log position
    # IS known (see reconstruct_hf_at_trigger, which always supplies it).
    oracle = _same_block_oracle(same_block_dir)
    ts = pd.Timestamp("2022-06-01T00:00:00", tz="UTC")

    price = oracle.price_at(RESERVE_A, ts)
    assert price == pytest.approx(300.0)


def test_prices_at_respects_log_index_ordering(same_block_dir):
    oracle = _same_block_oracle(same_block_dir)
    ts = pd.Timestamp("2022-06-01T00:00:00", tz="UTC")

    result = oracle.prices_at([RESERVE_A], ts, block_number=200, log_index=10)

    assert result[RESERVE_A] == pytest.approx(100.0)


def test_price_at_log_index_ordering_unaffected_across_different_blocks(
    chainlink_dir, feed_map
):
    # Cross-block results are always identical with or without log_index --
    # block_number order is a strict refinement of block_timestamp order (no
    # cross-block ties), see _merge_aggregator_series's docstring.
    oracle = _oracle(chainlink_dir, feed_map)
    ts = pd.Timestamp("2022-05-11T12:00:00", tz="UTC")

    without_log_index = oracle.price_at(RESERVE_A, ts)
    with_log_index = oracle.price_at(RESERVE_A, ts, block_number=200, log_index=999)

    assert without_log_index == pytest.approx(with_log_index)


# ---------------------------------------------------------------------------
# BlendedPriceOracle (CAS-28 Lever 2)
# ---------------------------------------------------------------------------


class _StubOracle:
    """Minimal oracle: fixed reserve -> price, missing reserves -> None."""

    def __init__(self, prices: dict[str, float]):
        self._prices = prices

    def price_at(
        self,
        address: str,
        timestamp: pd.Timestamp,
        block_number: int | None = None,
        log_index: int | None = None,
    ) -> float | None:
        return self._prices.get(address.lower())

    def prices_at(
        self,
        addresses: list[str],
        timestamp: pd.Timestamp,
        block_number: int | None = None,
        log_index: int | None = None,
    ) -> dict[str, float]:
        return {
            a.lower(): self._prices[a.lower()]
            for a in addresses
            if a.lower() in self._prices
        }

    def coverage(self) -> set[str]:
        return set(self._prices)


_TS = pd.Timestamp("2022-05-10T12:00:00", tz="UTC")


def test_blended_prefers_chainlink_where_available():
    # A is priced by both -> Chainlink wins; B only by DefiLlama -> fallback.
    blended = BlendedPriceOracle(
        chainlink=_StubOracle({RESERVE_A: 100.0}),
        defillama=_StubOracle({RESERVE_A: 90.0, RESERVE_B_ETH_QUOTED: 5.0}),
    )
    assert blended.price_at(RESERVE_A, _TS) == pytest.approx(100.0)  # chainlink
    assert blended.price_at(RESERVE_B_ETH_QUOTED, _TS) == pytest.approx(
        5.0
    )  # defillama

    both = blended.prices_at([RESERVE_A, RESERVE_B_ETH_QUOTED], _TS)
    assert both[RESERVE_A] == pytest.approx(100.0)
    assert both[RESERVE_B_ETH_QUOTED] == pytest.approx(5.0)


def test_blended_falls_back_when_chainlink_has_no_price():
    blended = BlendedPriceOracle(
        chainlink=_StubOracle({}),  # covered nowhere
        defillama=_StubOracle({RESERVE_A: 90.0}),
    )
    assert blended.price_at(RESERVE_A, _TS) == pytest.approx(90.0)


def test_blended_coverage_is_union():
    blended = BlendedPriceOracle(
        chainlink=_StubOracle({RESERVE_A: 100.0}),
        defillama=_StubOracle({RESERVE_B_ETH_QUOTED: 5.0}),
    )
    assert blended.coverage() == {RESERVE_A, RESERVE_B_ETH_QUOTED}


def test_blended_degrades_to_defillama_when_chainlink_lake_absent(monkeypatch):
    # No explicit chainlink + no lake on disk -> FileNotFoundError is
    # swallowed and the blend is DefiLlama-only (no-op-when-missing).
    def _raise(*args, **kwargs):
        raise FileNotFoundError("no chainlink lake")

    monkeypatch.setattr(prices_module, "ChainlinkPriceOracle", _raise)
    blended = BlendedPriceOracle(defillama=_StubOracle({RESERVE_A: 90.0}))
    assert blended._chainlink is None
    assert blended.price_at(RESERVE_A, _TS) == pytest.approx(90.0)
    assert blended.coverage() == {RESERVE_A}


# ---------------------------------------------------------------------------
# EthNumeraire (CAS-28 H1)
# ---------------------------------------------------------------------------

RESERVE_ETH_A = "0x000000000000000000000000000000000000d4"
UNCOVERED_ETH_RESERVE = "0x000000000000000000000000000000000000e5"
TEST_WETH_ADDRESS = "0x000000000000000000000000000000000000ee"

ETH_NUMERAIRE_AGGREGATOR = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
ETH_NUMERAIRE_AGGREGATOR_V2 = "0xffffffffffffffffffffffffffffffffffffff"  # migration


@pytest.fixture
def eth_numeraire_dir(tmp_path: Path) -> Path:
    rows = [
        # RESERVE_ETH_A: native asset/ETH feed, decimals=18, split across two
        # aggregators (a feed migration) to test series-merging.
        _row(
            ETH_NUMERAIRE_AGGREGATOR,
            "2022-05-10T00:00:00",
            int(0.1 * 10**18),
            block_number=100,
        ),
        _row(
            ETH_NUMERAIRE_AGGREGATOR,
            "2022-05-11T00:00:00",
            int(0.09 * 10**18),
            block_number=200,
        ),
        _row(
            ETH_NUMERAIRE_AGGREGATOR_V2,
            "2022-05-12T00:00:00",
            int(0.05 * 10**18),
            block_number=300,
        ),
        # Must be filtered out (not AnswerUpdated).
        _row(
            ETH_NUMERAIRE_AGGREGATOR,
            "2022-05-13T00:00:00",
            int(999 * 10**18),
            event_type="Other",
            block_number=400,
        ),
    ]
    df = pd.DataFrame(rows)
    path = tmp_path / "chainlink.parquet"
    df.to_parquet(path)
    return tmp_path


@pytest.fixture
def eth_numeraire_feed_map() -> dict:
    return {
        RESERVE_ETH_A: {
            "decimals": 18,
            "aggregators": [ETH_NUMERAIRE_AGGREGATOR, ETH_NUMERAIRE_AGGREGATOR_V2],
        },
    }


def _eth_numeraire(
    eth_numeraire_dir: Path, eth_numeraire_feed_map: dict
) -> EthNumeraire:
    return EthNumeraire(
        chainlink_dir=eth_numeraire_dir,
        feed_map=eth_numeraire_feed_map,
        weth_address=TEST_WETH_ADDRESS,
    )


def test_eth_numeraire_weth_is_always_one(eth_numeraire_dir, eth_numeraire_feed_map):
    oracle = _eth_numeraire(eth_numeraire_dir, eth_numeraire_feed_map)
    # WETH's price is the numeraire identity -- not a feed lookup -- so it's
    # 1.0 at any timestamp, including one long before any pulled data exists.
    assert oracle.price_at(
        TEST_WETH_ADDRESS, pd.Timestamp("2000-01-01", tz="UTC")
    ) == pytest.approx(1.0)
    assert oracle.price_at(
        TEST_WETH_ADDRESS, pd.Timestamp("2022-05-11T12:00:00", tz="UTC")
    ) == pytest.approx(1.0)


def test_eth_numeraire_coverage_includes_weth_identity(
    eth_numeraire_dir, eth_numeraire_feed_map
):
    oracle = _eth_numeraire(eth_numeraire_dir, eth_numeraire_feed_map)
    assert oracle.coverage() == {RESERVE_ETH_A, TEST_WETH_ADDRESS}


def test_eth_numeraire_nearest_prior_across_migrated_aggregators(
    eth_numeraire_dir, eth_numeraire_feed_map
):
    oracle = _eth_numeraire(eth_numeraire_dir, eth_numeraire_feed_map)
    # Between 05-11 (old aggregator) and 05-12 (new aggregator) -- nearest
    # prior should be the 05-11 value from the OLD aggregator, proving the
    # two addresses were merged into one series.
    price = oracle.price_at(
        RESERVE_ETH_A, pd.Timestamp("2022-05-11T12:00:00", tz="UTC")
    )
    assert price == pytest.approx(0.09)
    price_after_migration = oracle.price_at(
        RESERVE_ETH_A, pd.Timestamp("2022-05-12T06:00:00", tz="UTC")
    )
    assert price_after_migration == pytest.approx(0.05)


def test_eth_numeraire_returns_none_for_uncovered_reserve(
    eth_numeraire_dir, eth_numeraire_feed_map
):
    oracle = _eth_numeraire(eth_numeraire_dir, eth_numeraire_feed_map)
    assert oracle.price_at(UNCOVERED_ETH_RESERVE, pd.Timestamp.now(tz="UTC")) is None


def test_eth_numeraire_returns_none_past_max_staleness(
    eth_numeraire_dir, eth_numeraire_feed_map
):
    oracle = EthNumeraire(
        chainlink_dir=eth_numeraire_dir,
        feed_map=eth_numeraire_feed_map,
        weth_address=TEST_WETH_ADDRESS,
        max_staleness=pd.Timedelta(hours=6),
    )
    far_timestamp = pd.Timestamp("2022-05-13T12:00:00", tz="UTC")
    assert oracle.price_at(RESERVE_ETH_A, far_timestamp) is None
    # WETH is still exactly 1.0 regardless -- staleness never applies to the
    # numeraire identity.
    assert oracle.price_at(TEST_WETH_ADDRESS, far_timestamp) == pytest.approx(1.0)


def test_eth_numeraire_prices_at_batches_weth_and_reserve(
    eth_numeraire_dir, eth_numeraire_feed_map
):
    oracle = _eth_numeraire(eth_numeraire_dir, eth_numeraire_feed_map)
    result = oracle.prices_at(
        [TEST_WETH_ADDRESS, RESERVE_ETH_A, UNCOVERED_ETH_RESERVE],
        pd.Timestamp("2022-05-10T12:00:00", tz="UTC"),
    )
    assert set(result.keys()) == {TEST_WETH_ADDRESS, RESERVE_ETH_A}
    assert result[TEST_WETH_ADDRESS] == pytest.approx(1.0)
    assert result[RESERVE_ETH_A] == pytest.approx(0.1)


def test_eth_numeraire_missing_chainlink_dir_raises():
    with pytest.raises(FileNotFoundError):
        EthNumeraire(chainlink_dir="data/raw/does_not_exist_dir")


def test_eth_numeraire_default_has_no_staleness_limit(
    eth_numeraire_dir, eth_numeraire_feed_map
):
    # CAS-28 H2: Aave's on-chain latestAnswer() never checks staleness, so
    # the default must not reject a valid but very old prior point.
    oracle = _eth_numeraire(eth_numeraire_dir, eth_numeraire_feed_map)
    far_future = pd.Timestamp("2030-01-01", tz="UTC")  # years after last data point
    assert oracle.price_at(RESERVE_ETH_A, far_future) == pytest.approx(0.05)


def test_eth_numeraire_log_index_ordering_sees_earlier_same_block_update(tmp_path):
    # Same mechanism as ChainlinkPriceOracle (CAS-28 H4b), separate
    # implementation -- confirm it independently.
    rows = [
        _row(
            ETH_NUMERAIRE_AGGREGATOR,
            "2022-06-01T00:00:00",
            int(0.01 * 10**18),
            block_number=200,
            log_index=5,
        ),
        _row(
            ETH_NUMERAIRE_AGGREGATOR,
            "2022-06-01T00:00:00",
            int(0.03 * 10**18),
            block_number=200,
            log_index=15,
        ),
    ]
    path = tmp_path / "chainlink.parquet"
    pd.DataFrame(rows).to_parquet(path)
    feed_map = {
        RESERVE_ETH_A: {"decimals": 18, "aggregators": [ETH_NUMERAIRE_AGGREGATOR]}
    }
    oracle = EthNumeraire(
        chainlink_dir=tmp_path, feed_map=feed_map, weth_address=TEST_WETH_ADDRESS
    )
    ts = pd.Timestamp("2022-06-01T00:00:00", tz="UTC")

    price = oracle.price_at(RESERVE_ETH_A, ts, block_number=200, log_index=10)

    assert price == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# aggregator_eras clipping (CAS-28 Lever 11/12, 2026-07-22)
# ---------------------------------------------------------------------------

OLD_SOURCE_AGGREGATOR = "0x1111111111111111111111111111111111111a"
NEW_SOURCE_AGGREGATOR = "0x2222222222222222222222222222222222222b"
_MIGRATION_BLOCK = 300


def test_eth_numeraire_aggregator_eras_clips_deprecated_source_bleed(tmp_path):
    # The deprecated OLD_SOURCE keeps emitting AnswerUpdated even after Aave
    # switched its real source to NEW_SOURCE at _MIGRATION_BLOCK (confirmed
    # real behavior for DAI etc., see chainlink_feeds.py) -- without
    # clipping, a later OLD_SOURCE row would wrongly outrank NEW_SOURCE's
    # answer in nearest-prior lookup.
    rows = [
        _row(
            OLD_SOURCE_AGGREGATOR,
            "2022-01-01T00:00:00",
            int(1.0 * 10**18),
            block_number=100,
        ),
        # OLD_SOURCE's "bleed" row -- emitted AFTER the migration, must be
        # excluded once clipped.
        _row(
            OLD_SOURCE_AGGREGATOR,
            "2022-01-03T00:00:00",
            int(1.5 * 10**18),
            block_number=350,
        ),
        _row(
            NEW_SOURCE_AGGREGATOR,
            "2022-01-02T00:00:00",
            int(2.0 * 10**18),
            block_number=_MIGRATION_BLOCK,
        ),
    ]
    path = tmp_path / "chainlink.parquet"
    pd.DataFrame(rows).to_parquet(path)
    feed_map = {
        RESERVE_ETH_A: {
            "decimals": 18,
            "aggregators": [OLD_SOURCE_AGGREGATOR, NEW_SOURCE_AGGREGATOR],
            "aggregator_eras": {
                OLD_SOURCE_AGGREGATOR: (None, _MIGRATION_BLOCK),
                NEW_SOURCE_AGGREGATOR: (_MIGRATION_BLOCK, None),
            },
        },
    }
    oracle = EthNumeraire(
        chainlink_dir=tmp_path, feed_map=feed_map, weth_address=TEST_WETH_ADDRESS
    )

    # Nearest-prior at block 400 (block_timestamp irrelevant here -- query by
    # block_number/log_index): the clipped merge must resolve to NEW_SOURCE's
    # 2.0 answer, not OLD_SOURCE's later (but deprecated) 1.5 bleed row.
    price = oracle.price_at(
        RESERVE_ETH_A,
        pd.Timestamp("2022-01-04T00:00:00", tz="UTC"),
        block_number=400,
        log_index=0,
    )
    assert price == pytest.approx(2.0)


def test_eth_numeraire_aggregator_eras_unclipped_shows_the_bleed_bug(tmp_path):
    # Same fixture as above, but WITHOUT aggregator_eras -- documents the bug
    # the clipping fixes: unclipped, the deprecated source's later row wins.
    rows = [
        _row(
            OLD_SOURCE_AGGREGATOR,
            "2022-01-01T00:00:00",
            int(1.0 * 10**18),
            block_number=100,
        ),
        _row(
            OLD_SOURCE_AGGREGATOR,
            "2022-01-03T00:00:00",
            int(1.5 * 10**18),
            block_number=350,
        ),
        _row(
            NEW_SOURCE_AGGREGATOR,
            "2022-01-02T00:00:00",
            int(2.0 * 10**18),
            block_number=_MIGRATION_BLOCK,
        ),
    ]
    path = tmp_path / "chainlink.parquet"
    pd.DataFrame(rows).to_parquet(path)
    feed_map = {
        RESERVE_ETH_A: {
            "decimals": 18,
            "aggregators": [OLD_SOURCE_AGGREGATOR, NEW_SOURCE_AGGREGATOR],
        },
    }
    oracle = EthNumeraire(
        chainlink_dir=tmp_path, feed_map=feed_map, weth_address=TEST_WETH_ADDRESS
    )
    price = oracle.price_at(
        RESERVE_ETH_A,
        pd.Timestamp("2022-01-04T00:00:00", tz="UTC"),
        block_number=400,
        log_index=0,
    )
    assert price == pytest.approx(1.5)


def test_eth_numeraire_aggregator_eras_omitted_is_backward_compatible(
    eth_numeraire_dir, eth_numeraire_feed_map
):
    # No reserve in this feed_map has `aggregator_eras` at all -- confirms
    # `spec.get("aggregator_eras")` degrades to the pre-existing unclipped
    # behavior exactly (same assertion as the migrated-aggregator test
    # above), not just that it doesn't crash.
    oracle = _eth_numeraire(eth_numeraire_dir, eth_numeraire_feed_map)
    price = oracle.price_at(
        RESERVE_ETH_A, pd.Timestamp("2022-05-12T06:00:00", tz="UTC")
    )
    assert price == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# coverage_end (CAS-28 Lever 11, 2026-07-22): a reserve-level cutoff for when
# Aave's own AssetSourceUpdated history shows it switched away from every
# mapped aggregator to an unpriceable source (e.g. WBTC's 2023 wBTC/BTC/ETH
# wrapper, which emits no AnswerUpdated events at all).
# ---------------------------------------------------------------------------

_COVERAGE_END_BLOCK = 500


def test_eth_numeraire_coverage_end_returns_none_past_the_cutoff(tmp_path):
    # The mapped aggregator keeps emitting past the real cutoff (same "old
    # source keeps limping along" shape as the aggregator_eras bleed case),
    # but Aave itself stopped reading this reserve's ETH feed entirely at
    # _COVERAGE_END_BLOCK -- past it, there is no known-correct price, so
    # this must return None rather than silently reusing the stale answer.
    rows = [
        _row(
            ETH_NUMERAIRE_AGGREGATOR,
            "2022-01-01T00:00:00",
            int(1.0 * 10**18),
            block_number=100,
        ),
        _row(
            ETH_NUMERAIRE_AGGREGATOR,
            "2022-01-05T00:00:00",
            int(1.5 * 10**18),
            block_number=600,
        ),
    ]
    path = tmp_path / "chainlink.parquet"
    pd.DataFrame(rows).to_parquet(path)
    feed_map = {
        RESERVE_ETH_A: {
            "decimals": 18,
            "aggregators": [ETH_NUMERAIRE_AGGREGATOR],
            "coverage_end": _COVERAGE_END_BLOCK,
        },
    }
    oracle = EthNumeraire(
        chainlink_dir=tmp_path, feed_map=feed_map, weth_address=TEST_WETH_ADDRESS
    )

    before_cutoff = oracle.price_at(
        RESERVE_ETH_A,
        pd.Timestamp("2022-01-02T00:00:00", tz="UTC"),
        block_number=200,
        log_index=0,
    )
    at_cutoff = oracle.price_at(
        RESERVE_ETH_A,
        pd.Timestamp("2022-01-06T00:00:00", tz="UTC"),
        block_number=_COVERAGE_END_BLOCK,
        log_index=0,
    )
    past_cutoff = oracle.price_at(
        RESERVE_ETH_A,
        pd.Timestamp("2022-01-07T00:00:00", tz="UTC"),
        block_number=700,
        log_index=0,
    )

    assert before_cutoff == pytest.approx(1.0)
    assert at_cutoff is None
    assert past_cutoff is None


def test_eth_numeraire_coverage_end_not_enforced_without_block_number(tmp_path):
    # coverage_end is a block boundary, not a timestamp one -- a caller that
    # only passes `timestamp` (no block_number) gets the pre-coverage_end
    # nearest-prior behavior, same as any other block_number-optional param
    # in this module.
    rows = [
        _row(
            ETH_NUMERAIRE_AGGREGATOR,
            "2022-01-05T00:00:00",
            int(1.5 * 10**18),
            block_number=600,
        ),
    ]
    path = tmp_path / "chainlink.parquet"
    pd.DataFrame(rows).to_parquet(path)
    feed_map = {
        RESERVE_ETH_A: {
            "decimals": 18,
            "aggregators": [ETH_NUMERAIRE_AGGREGATOR],
            "coverage_end": _COVERAGE_END_BLOCK,
        },
    }
    oracle = EthNumeraire(
        chainlink_dir=tmp_path, feed_map=feed_map, weth_address=TEST_WETH_ADDRESS
    )

    price = oracle.price_at(
        RESERVE_ETH_A, pd.Timestamp("2022-01-06T00:00:00", tz="UTC")
    )

    assert price == pytest.approx(1.5)


def test_eth_numeraire_coverage_end_omitted_is_backward_compatible(
    eth_numeraire_dir, eth_numeraire_feed_map
):
    # No reserve in this feed_map has `coverage_end` at all -- confirms
    # `spec.get("coverage_end")` degrades to the pre-existing unbounded
    # behavior exactly, not just that it doesn't crash.
    oracle = _eth_numeraire(eth_numeraire_dir, eth_numeraire_feed_map)
    price = oracle.price_at(
        RESERVE_ETH_A,
        pd.Timestamp("2022-05-12T06:00:00", tz="UTC"),
        block_number=10_000_000,
        log_index=0,
    )
    assert price == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# PreferEthNumeraireOracle (CAS-28 H1)
# ---------------------------------------------------------------------------


def test_prefer_eth_uses_eth_prices_when_position_fully_covered():
    eth = _StubOracle({RESERVE_A: 1.0, RESERVE_B_ETH_QUOTED: 2.0})
    blended = _StubOracle({RESERVE_A: 100.0, RESERVE_B_ETH_QUOTED: 200.0})
    oracle = PreferEthNumeraireOracle(eth=eth, blended=blended)

    result = oracle.prices_at([RESERVE_A, RESERVE_B_ETH_QUOTED], _TS)

    assert result == {
        RESERVE_A: pytest.approx(1.0),
        RESERVE_B_ETH_QUOTED: pytest.approx(2.0),
    }


def test_prefer_eth_falls_back_to_blended_for_whole_position_when_any_leg_missing():
    # RESERVE_B has no ETH coverage -- numeraires must never mix, so RESERVE_A
    # must ALSO come from blended (USD), not from eth even though eth covers it.
    eth = _StubOracle({RESERVE_A: 1.0})
    blended = _StubOracle({RESERVE_A: 100.0, RESERVE_B_ETH_QUOTED: 200.0})
    oracle = PreferEthNumeraireOracle(eth=eth, blended=blended)

    result = oracle.prices_at([RESERVE_A, RESERVE_B_ETH_QUOTED], _TS)

    assert result == {
        RESERVE_A: pytest.approx(100.0),
        RESERVE_B_ETH_QUOTED: pytest.approx(200.0),
    }


def test_prefer_eth_price_at_prefers_eth_then_falls_back():
    eth = _StubOracle({RESERVE_A: 1.0})
    blended = _StubOracle({RESERVE_A: 100.0, RESERVE_B_ETH_QUOTED: 200.0})
    oracle = PreferEthNumeraireOracle(eth=eth, blended=blended)

    assert oracle.price_at(RESERVE_A, _TS) == pytest.approx(1.0)  # eth wins
    assert oracle.price_at(RESERVE_B_ETH_QUOTED, _TS) == pytest.approx(
        200.0
    )  # fallback


def test_prefer_eth_coverage_is_union():
    eth = _StubOracle({RESERVE_A: 1.0})
    blended = _StubOracle({RESERVE_B_ETH_QUOTED: 200.0})
    oracle = PreferEthNumeraireOracle(eth=eth, blended=blended)

    assert oracle.coverage() == {RESERVE_A, RESERVE_B_ETH_QUOTED}


def test_prefer_eth_degrades_to_blended_when_chainlink_lake_absent(monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("no chainlink lake")

    monkeypatch.setattr(prices_module, "EthNumeraire", _raise)
    blended = _StubOracle({RESERVE_A: 90.0})
    oracle = PreferEthNumeraireOracle(blended=blended)

    assert oracle._eth is None
    assert oracle.prices_at([RESERVE_A], _TS) == {RESERVE_A: pytest.approx(90.0)}
    assert oracle.coverage() == {RESERVE_A}


def test_prefer_eth_empty_addresses_returns_empty():
    oracle = PreferEthNumeraireOracle(
        eth=_StubOracle({}), blended=_StubOracle({RESERVE_A: 90.0})
    )
    assert oracle.prices_at([], _TS) == {}


class _RecordingStubOracle(_StubOracle):
    """Same contract as `_StubOracle`, but remembers the last call's
    `block_number`/`log_index` so callers can assert they were threaded
    through (CAS-28 H4b), not silently dropped by an intermediate wrapper."""

    def __init__(self, prices: dict[str, float]):
        super().__init__(prices)
        self.last_call: tuple[int | None, int | None] | None = None

    def prices_at(self, addresses, timestamp, block_number=None, log_index=None):
        self.last_call = (block_number, log_index)
        return super().prices_at(addresses, timestamp, block_number, log_index)


def test_prefer_eth_threads_block_number_and_log_index_to_eth_oracle():
    eth = _RecordingStubOracle({RESERVE_A: 1.0})
    blended = _StubOracle({RESERVE_A: 100.0})
    oracle = PreferEthNumeraireOracle(eth=eth, blended=blended)

    oracle.prices_at([RESERVE_A], _TS, block_number=12345, log_index=7)

    assert eth.last_call == (12345, 7)


def test_blended_threads_block_number_and_log_index_to_chainlink():
    chainlink = _RecordingStubOracle({RESERVE_A: 100.0})
    blended = BlendedPriceOracle(
        chainlink=chainlink, defillama=_StubOracle({RESERVE_A: 90.0})
    )

    blended.prices_at([RESERVE_A], _TS, block_number=12345, log_index=7)

    assert chainlink.last_call == (12345, 7)


# ---------------------------------------------------------------------------
# Real-data smoke test (skips cleanly without the LFS-pulled data lake)
# ---------------------------------------------------------------------------


def _has_real_parquet(directory: Path) -> bool:
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


_HAS_REAL_DATA = _has_real_parquet(Path("data/raw/chainlink"))


@pytest.mark.skipif(
    not _HAS_REAL_DATA, reason="requires ingested data/raw/chainlink (Git LFS pull)"
)
def test_real_data_loads_and_covers_expected_reserve_count():
    oracle = ChainlinkPriceOracle()
    # 32 of Aave v2's 37 reserves have a matched raw Chainlink feed -- see
    # chainlink_feeds.py's module docstring for the other 5.
    assert len(oracle.coverage()) == 32


@pytest.mark.skipif(
    not _HAS_REAL_DATA, reason="requires ingested data/raw/chainlink (Git LFS pull)"
)
def test_real_data_eth_numeraire_covers_expected_reserve_count():
    oracle = EthNumeraire()
    # All 37 of Aave v2's reserves now have a matched asset/ETH feed (30 from
    # H1/H2 + stETH/CVX from Lever 11 + GUSD/ENS/LUSD from Lever 11b +
    # xSUSHI from Lever 11c, all custom-adapter cross-rate/share-price
    # replications), + WETH's numeraire identity -- see chainlink_feeds.py's
    # module docstring; `UNCOVERED_ETH_RESERVES` is now empty. Note
    # `coverage()` doesn't account for `coverage_end` (WBTC/stETH/LUSD still
    # count as "covered" even though queries past their real cutoff return
    # `None` from `price_at`) -- see EthNumeraire.coverage's docstring.
    assert len(oracle.coverage()) == 37
