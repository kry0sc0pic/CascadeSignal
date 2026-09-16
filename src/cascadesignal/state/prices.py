"""Price lookups for state reconstruction (CAS-13/CAS-47).

Three oracles:

- `PriceOracle`: DefiLlama daily USD prices (`data/raw/defillama/prices/`).
  Full study-period coverage, but only day-level resolution -- a liquidation
  and the price used to evaluate it may be up to ~24h apart.
- `ChainlinkPriceOracle` (CAS-17): Chainlink `AnswerUpdated` events
  (`data/raw/chainlink/chain=1/`), matched to reserves via
  `chainlink_feeds.RESERVE_CHAINLINK_FEEDS`. Real block-timestamp
  resolution, but partial coverage (golden-episode + surgical-backfill
  windows, not the full study period) and missing 5 of 37 reserves entirely
  (see `chainlink_feeds.UNCOVERED_RESERVES`).
- `BlendedPriceOracle` (CAS-28 Lever 2): Chainlink block-level price per
  reserve where available (within its staleness), DefiLlama daily otherwise
  -- the best available price signal for each reserve at each block. This is
  also the more *correct* oracle for HF reconstruction: Chainlink is the
  feed Aave v2 itself read to make its liquidation decisions, so where it's
  present it's the actual decision-relevant price, not a daily proxy. Using
  it as the T2 gate's primary oracle moves the mismatch rate 30.9% -> 24.2%.
- `EthNumeraire` (CAS-28 H1): prices every reserve natively in ETH via its
  own asset/ETH Chainlink feed, instead of reconstructing an implied
  asset/ETH cross-rate from two independent asset/USD feeds. `PriceOracle`/
  `ChainlinkPriceOracle`/`BlendedPriceOracle` are all USD-denominated; this
  is the one ETH-denominated oracle, and must not be mixed with a
  USD-denominated one within a single HF computation (see
  `PreferEthNumeraireOracle`).
- `PreferEthNumeraireOracle` (CAS-28 H1): per-position wrapper -- uses
  `EthNumeraire` only when it covers every reserve touched by that position,
  otherwise falls back to `BlendedPriceOracle` (USD) for the whole position.
  This is the T2 gate's current primary oracle.
"""

from __future__ import annotations

import glob
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from cascadesignal.state.chainlink_feeds import (
    ETH_USD_AGGREGATORS,
    RESERVE_CHAINLINK_ETH_FEEDS,
    RESERVE_CHAINLINK_FEEDS,
    WETH_ADDRESS,
)

_DEFAULT_PRICES_PATH = Path("data/raw/defillama/prices/token_prices_daily.parquet")
_DEFAULT_CHAINLINK_DIR = Path("data/raw/chainlink/chain=1")


class PriceOracle:
    """Nearest-prior-day USD price lookup per reserve address."""

    def __init__(self, prices_path: str | Path = _DEFAULT_PRICES_PATH):
        df = pd.read_parquet(prices_path)
        df = df.sort_values("date")
        self._by_address: dict[str, pd.DataFrame] = {
            str(address): group[["date", "price"]].reset_index(drop=True)
            for address, group in df.groupby("address")
        }

    def price_at(
        self,
        address: str,
        timestamp: pd.Timestamp,
        block_number: int | None = None,
        log_index: int | None = None,
    ) -> float | None:
        """Nearest available daily price at or before `timestamp`.

        Falls back to the earliest available price if `timestamp` predates
        coverage (e.g. a token deployed after the study period's start but
        queried near it), rather than returning None outright.

        `block_number`/`log_index` (CAS-28 H4b): accepted for interface
        parity with `PriceOracleLike` but unused -- daily granularity has no
        intra-day order to resolve against, so this is a no-op, same as
        every other CAS-28 correction's no-op-when-inapplicable convention.
        """
        if not address:
            return None
        series = self._by_address.get(address.lower())
        if series is None or series.empty:
            return None
        idx = int(series["date"].searchsorted(timestamp, side="right")) - 1
        if idx < 0:
            idx = 0
        return float(series.iloc[idx]["price"])

    def prices_at(
        self,
        addresses: list[str],
        timestamp: pd.Timestamp,
        block_number: int | None = None,
        log_index: int | None = None,
    ) -> dict[str, float]:
        out = {}
        for address in addresses:
            price = self.price_at(address, timestamp, block_number, log_index)
            if price is not None:
                out[address.lower()] = price
        return out

    def coverage(self) -> set[str]:
        """Addresses with at least one available daily price point."""
        return set(self._by_address.keys())


_DEFAULT_MAX_STALENESS = pd.Timedelta(days=3)

# CAS-28 H4b: combined sortable (block_number, log_index) order key, used to
# resolve "last price strictly before THIS log position" instead of "last
# price at or before this timestamp" -- every tx in a block shares one
# `block_timestamp`, so timestamp-only resolution can't distinguish intra-
# block order (see `ChainlinkPriceOracle`/`EthNumeraire`'s `price_at`).
# Ethereum blocks are nowhere near 1M logs (gas-limited to a small fraction
# of that even in the most log-dense real block), so this multiplier can
# never collide two distinct (block_number, log_index) pairs.
_ORDER_KEY_LOG_INDEX_MULTIPLIER = 1_000_000


def _order_key(block_number, log_index):
    return block_number * _ORDER_KEY_LOG_INDEX_MULTIPLIER + log_index


def _merge_aggregator_series(
    raw: pd.DataFrame,
    aggregators: list[str],
    decimals: int,
    aggregator_eras: dict[str, tuple[int | None, int | None]] | None = None,
) -> pd.DataFrame:
    """Combine every aggregator address for one logical feed into one series.

    Chainlink migrates feeds to new proxy addresses periodically, so a
    single feed's full history is often split across 2-4 contracts sharing
    one `description()` -- see `chainlink_feeds.py`'s module docstring.
    Shared by `ChainlinkPriceOracle` (asset/USD) and `EthNumeraire`
    (asset/ETH); both key off the same raw `AnswerUpdated` event table, just
    a different feed_map.

    Sorted/deduped by `(block_number, log_index)` (CAS-28 H4b), not
    `block_timestamp` alone -- block_number order is a strict refinement of
    block_timestamp order (Ethereum block timestamps are strictly increasing
    with block_number, no cross-block ties), so this changes nothing for any
    existing block_timestamp-only consumer, but preserves >1 same-block
    update to the same feed (previously collapsed via `keep="last"` on
    `block_timestamp` alone, which would have silently discarded an earlier
    same-block reading that `price_at`'s new log_index-aware resolution
    needs to be able to see).

    `aggregator_eras` (CAS-28 Lever 11/12, 2026-07-22): optional per-address
    `(era_start_block, era_end_block)` clip, either bound `None` for
    unbounded. Exists because Aave switching its *top-level* oracle source
    away from an address (confirmed via `AaveOracle.AssetSourceUpdated`,
    `scripts/onchain/backfill_aave_oracle_sources.py`) does not stop that
    address's own Chainlink aggregator from continuing to emit answers --
    e.g. DAI's pre-2024 aggregators kept emitting for over a year past Aave's
    real 2024-04-24 migration to a new source. Without clipping, a
    deprecated aggregator's later answer can silently outrank the new
    source's in the merged nearest-prior series -- exactly the "phase
    bleed" this ticket's Lever 12 describes. `None` (the default) reproduces
    the pre-existing unclipped-merge behavior exactly; only populated for
    reserves in `chainlink_feeds.RESERVE_CHAINLINK_ETH_FEEDS` where a real
    per-era divergence was confirmed, not speculatively for every reserve.
    """
    sub = raw[raw["user"].isin([a.lower() for a in aggregators])]
    if aggregator_eras:
        keep = pd.Series(True, index=sub.index)
        for addr, (era_start, era_end) in aggregator_eras.items():
            is_this_agg = sub["user"] == addr.lower()
            if era_start is not None:
                keep &= ~(is_this_agg & (sub["block_number"] < era_start))
            if era_end is not None:
                keep &= ~(is_this_agg & (sub["block_number"] >= era_end))
        sub = sub[keep]
    if sub.empty:
        return pd.DataFrame(
            columns=["block_timestamp", "block_number", "log_index", "price"]
        )
    out = pd.DataFrame(
        {
            "block_timestamp": sub["block_timestamp"].to_numpy(),
            "block_number": sub["block_number"].to_numpy(),
            "log_index": sub["log_index"].to_numpy(),
            "price": (sub["value"] / (10.0**decimals)).to_numpy(),
        }
    )
    return (
        out.sort_values(["block_number", "log_index"])
        .drop_duplicates(subset=["block_number", "log_index"], keep="last")
        .reset_index(drop=True)
    )


def _load_answer_updated(chainlink_dir: str | Path) -> pd.DataFrame:
    """Load + normalize the raw `AnswerUpdated` event table shared by every
    Chainlink-backed oracle in this module. Raises `FileNotFoundError` if the
    lake hasn't been pulled -- callers rely on this for the no-op-when-missing
    degrade convention (see `BlendedPriceOracle`/`PreferEthNumeraireOracle`)."""
    paths = sorted(glob.glob(str(Path(chainlink_dir) / "*.parquet")))
    if not paths:
        raise FileNotFoundError(
            f"No Chainlink parquet files found under {chainlink_dir}"
        )
    raw = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
    raw = raw[raw["event_type"] == "AnswerUpdated"][
        ["user", "block_number", "log_index", "block_timestamp", "amount_raw"]
    ].copy()
    raw["user"] = raw["user"].str.lower()
    raw["value"] = pd.to_numeric(raw["amount_raw"], errors="coerce")
    return raw.dropna(subset=["value"])


class ChainlinkPriceOracle:
    """Nearest-prior-block-timestamp USD price lookup via Chainlink `AnswerUpdated`
    events, restricted to the 5 golden-episode pull windows (CAS-17).

    Unlike `PriceOracle`, this does NOT fall back to the nearest available
    point when `timestamp` falls in a coverage gap -- the 5 pulled windows
    are separated by months to years, so "nearest prior" across a gap would
    silently return a stale price from a different episode entirely. Instead,
    `price_at` returns `None` once the gap to the nearest prior point exceeds
    `max_staleness`, matching `PriceOracle.prices_at`'s existing contract
    that a missing price surfaces as `fully_covered=False` rather than a
    silently wrong number (see `health_factor.compute_health_factor`).

    Reserve coverage is also partial: `chainlink_feeds.RESERVE_CHAINLINK_FEEDS`
    covers 32 of Aave v2's 37 reserves (5 -- GUSD, xSUSHI, stETH, ENS, CVX --
    have no matching raw Chainlink feed in the pulled data; see that module's
    docstring). Reserves not in `coverage()` always return `None`.
    """

    def __init__(
        self,
        chainlink_dir: str | Path = _DEFAULT_CHAINLINK_DIR,
        feed_map: dict[str, dict] | None = None,
        eth_usd_aggregators: list[str] | None = None,
        max_staleness: pd.Timedelta = _DEFAULT_MAX_STALENESS,
    ):
        feed_map = RESERVE_CHAINLINK_FEEDS if feed_map is None else feed_map
        eth_usd_aggregators = (
            ETH_USD_AGGREGATORS if eth_usd_aggregators is None else eth_usd_aggregators
        )
        self._max_staleness = max_staleness

        raw = _load_answer_updated(chainlink_dir)
        eth_usd_series = _merge_aggregator_series(raw, eth_usd_aggregators, decimals=8)

        self._by_address: dict[str, pd.DataFrame] = {}
        for reserve, spec in feed_map.items():
            series = _merge_aggregator_series(
                raw, spec["aggregators"], decimals=spec["decimals"]
            )
            if series.empty:
                continue
            if spec["quote"] == "ETH":
                series = self._convert_via_eth_usd(series, eth_usd_series)
                # `_convert_via_eth_usd`'s merge_asof sorts by
                # `block_timestamp` alone (default unstable quicksort) --
                # every event in a block shares one timestamp, so that sort
                # can scramble the (already-correct) intra-block log_index
                # order `_merge_aggregator_series` produced. Re-establish it
                # before relying on `_order_key` being monotonic below.
                series = series.sort_values(
                    ["block_number", "log_index"], kind="mergesort"
                )
            series = series[
                ["block_timestamp", "block_number", "log_index", "price"]
            ].reset_index(drop=True)
            series["_order_key"] = _order_key(
                series["block_number"].to_numpy(), series["log_index"].to_numpy()
            )
            self._by_address[reserve.lower()] = series

    @staticmethod
    def _convert_via_eth_usd(
        eth_quoted: pd.DataFrame, eth_usd_series: pd.DataFrame
    ) -> pd.DataFrame:
        """asset/ETH series -> asset/USD, via nearest-prior ETH/USD price.

        Only `block_timestamp` + the renamed price column are taken from
        `eth_usd_series` -- it's just the conversion-rate lookup (as of when
        the asset/ETH reading itself was made, an unrelated join, not the
        HF-reconstruction query point CAS-28 H4b's log_index ordering is
        about), and both sides otherwise carry `block_number`/`log_index`
        columns that would collide (get `_x`/`_y`-suffixed) if not narrowed
        first -- `eth_quoted`'s own (the ones that actually matter to the
        caller) must survive unsuffixed.
        """
        if eth_usd_series.empty:
            return pd.DataFrame(
                columns=["block_timestamp", "block_number", "log_index", "price"]
            )
        merged = pd.merge_asof(
            eth_quoted.sort_values("block_timestamp"),
            eth_usd_series[["block_timestamp", "price"]]
            .sort_values("block_timestamp")
            .rename(columns={"price": "eth_usd_price"}),
            on="block_timestamp",
            direction="backward",
        )
        merged = merged.dropna(subset=["eth_usd_price"])
        merged["price"] = merged["price"] * merged["eth_usd_price"]
        return merged

    def price_at(
        self,
        address: str,
        timestamp: pd.Timestamp,
        block_number: int | None = None,
        log_index: int | None = None,
    ) -> float | None:
        """Nearest available price strictly before `(block_number,
        log_index)` if both are given (CAS-28 H4b), else at-or-before
        `timestamp` alone (pre-H4b behavior).

        The two only differ within a single block: every tx in a block
        shares one `block_timestamp`, so a same-block price update landing
        *after* the query's own log position is (correctly) invisible under
        `(block_number, log_index)` ordering but was previously (wrongly)
        visible under timestamp-only ordering's tie-inclusive
        `searchsorted(..., side="right")`. Cross-block results are always
        identical (block_number order is a strict refinement of
        block_timestamp order -- see `_merge_aggregator_series`).

        Returns `None` (not a stale guess) if there's no prior point at all,
        or if the nearest prior point is more than `max_staleness` away --
        see class docstring.
        """
        if not address:
            return None
        series = self._by_address.get(address.lower())
        if series is None or series.empty:
            return None
        if block_number is not None and log_index is not None:
            query_key = _order_key(block_number, log_index)
            idx = int(series["_order_key"].searchsorted(query_key, side="right")) - 1
        else:
            idx = (
                int(series["block_timestamp"].searchsorted(timestamp, side="right")) - 1
            )
        if idx < 0:
            return None
        row = series.iloc[idx]
        if timestamp - row["block_timestamp"] > self._max_staleness:
            return None
        return float(row["price"])

    def prices_at(
        self,
        addresses: list[str],
        timestamp: pd.Timestamp,
        block_number: int | None = None,
        log_index: int | None = None,
    ) -> dict[str, float]:
        out = {}
        for address in addresses:
            price = self.price_at(address, timestamp, block_number, log_index)
            if price is not None:
                out[address.lower()] = price
        return out

    def coverage(self) -> set[str]:
        """Reserve addresses with at least one available price point."""
        return set(self._by_address.keys())

    def prices_at_many(
        self, address: str, timestamps: pd.Series | np.ndarray
    ) -> np.ndarray:
        """Vectorized `price_at` over many timestamps for one reserve (CAS-49).

        Same nearest-prior-block-timestamp + `max_staleness` contract as
        `price_at`, via `np.searchsorted` instead of a per-timestamp Python
        loop -- ~1000x faster for bar-matrix-scale joins (millions of rows)
        where `price_at` in a loop would otherwise dominate runtime.
        """
        n = len(timestamps)
        out = np.full(n, np.nan)
        series = self._by_address.get(address.lower())
        if series is None or series.empty:
            return out

        ts = pd.to_datetime(pd.Series(timestamps), utc=True).to_numpy()
        feed_ts = pd.to_datetime(series["block_timestamp"], utc=True).to_numpy()
        idx = np.searchsorted(feed_ts, ts, side="right") - 1
        valid = idx >= 0
        prices = series["price"].to_numpy(dtype=float)
        out[valid] = prices[idx[valid]]

        staleness = ts[valid] - feed_ts[np.clip(idx[valid], 0, len(feed_ts) - 1)]
        too_stale = staleness > np.timedelta64(self._max_staleness)
        stale_positions = np.flatnonzero(valid)[too_stale]
        out[stale_positions] = np.nan
        return out


class BlendedPriceOracle:
    """Best-available price per reserve: Chainlink block-level where covered
    (within its staleness window), DefiLlama daily otherwise (CAS-28 Lever 2).

    This is the T2 gate's preferred primary oracle. Two reasons it beats
    either source alone:

    1. Correctness -- Chainlink is the feed Aave v2 read to trigger the very
       liquidations the gate reconstructs, so where it's present it's the
       actual decision-relevant price, not DefiLlama's daily proxy.
    2. Coverage -- taking the union means a reserve missing from one source
       (a DefiLlama gap, or a block outside Chainlink's pulled windows) is
       still priced from the other, so more positions are `fully_covered`.

    Degrades gracefully to DefiLlama-only if the Chainlink lake hasn't been
    pulled (`ChainlinkPriceOracle` raises `FileNotFoundError`), the same
    no-op-when-missing convention as the engine's correction tables -- so a
    checkout without `data/raw/chainlink/` still reconstructs, just at daily
    resolution.
    """

    def __init__(
        self,
        chainlink: ChainlinkPriceOracle | None = None,
        defillama: PriceOracle | None = None,
    ):
        self._defillama = PriceOracle() if defillama is None else defillama
        if chainlink is not None:
            self._chainlink: ChainlinkPriceOracle | None = chainlink
        else:
            try:
                self._chainlink = ChainlinkPriceOracle()
            except FileNotFoundError:
                self._chainlink = None

    def price_at(
        self,
        address: str,
        timestamp: pd.Timestamp,
        block_number: int | None = None,
        log_index: int | None = None,
    ) -> float | None:
        if self._chainlink is not None:
            chainlink_price = self._chainlink.price_at(
                address, timestamp, block_number, log_index
            )
            if chainlink_price is not None:
                return chainlink_price
        return self._defillama.price_at(address, timestamp)

    def prices_at(
        self,
        addresses: list[str],
        timestamp: pd.Timestamp,
        block_number: int | None = None,
        log_index: int | None = None,
    ) -> dict[str, float]:
        out = self._defillama.prices_at(addresses, timestamp)
        if self._chainlink is not None:
            out.update(
                self._chainlink.prices_at(addresses, timestamp, block_number, log_index)
            )
        return out

    def coverage(self) -> set[str]:
        """Union of both sources' covered reserve addresses."""
        cov = self._defillama.coverage()
        if self._chainlink is not None:
            cov = cov | self._chainlink.coverage()
        return cov


_NO_STALENESS_LIMIT = pd.Timedelta.max


class EthNumeraire:
    """Nearest-prior-block-timestamp ETH-denominated price lookup via each
    reserve's own Chainlink asset/ETH feed (CAS-28 H1/H2).

    Aave v2's `calculateUserAccountData` prices every reserve in ETH -- WETH
    is the protocol's numeraire, so its price is exactly 1.0 by construction,
    never a feed read (`price_at`/`prices_at` special-case
    `chainlink_feeds.WETH_ADDRESS` rather than looking it up in `coverage()`).
    Every other reserve is looked up via `chainlink_feeds.RESERVE_CHAINLINK_ETH_FEEDS`.

    This is deliberately NOT USD-denominated, unlike every other oracle in
    this module -- callers must not mix an `EthNumeraire` price for one
    position leg with a USD-denominated price for another within the same HF
    ratio (see `PreferEthNumeraireOracle`, which enforces this per-position).

    Nearest-prior-block-timestamp lookup, same shape as `ChainlinkPriceOracle`,
    but **no staleness guard by default** (CAS-28 H2): the on-chain
    `latestAnswer()` Aave v2 actually reads never checks the age of the last
    stored answer -- it returns whatever was last written, however old, with
    no revert. `ChainlinkPriceOracle`'s 3-day `max_staleness` exists to avoid
    bridging across the (months-to-years) gaps *between* the 5 golden-episode
    pull windows; H2's full-history pull (`data/raw/chainlink/chain=1/eth_feeds_full_history_answer_updated.parquet`)
    removes that gap for the 30 covered reserves, so rejecting an old-but-real
    price here would be *infidelity* to Aave's actual read, not a safety net.
    `price_at` still returns `None` when there's no prior point at all (a true
    coverage gap); pass an explicit `max_staleness` to restore the guard (e.g.
    for a checkout that only has the golden-window-only pull). Reserve
    coverage is all 37 (+ WETH's identity) as of Lever 11c -- see
    `chainlink_feeds.UNCOVERED_ETH_RESERVES` (now empty) and
    `RESERVE_CHAINLINK_ETH_FEEDS`'s module docstring for how the last six
    (stETH, CVX, GUSD, ENS, LUSD, xSUSHI) were closed.
    """

    def __init__(
        self,
        chainlink_dir: str | Path = _DEFAULT_CHAINLINK_DIR,
        feed_map: dict[str, dict] | None = None,
        weth_address: str = WETH_ADDRESS,
        max_staleness: pd.Timedelta = _NO_STALENESS_LIMIT,
    ):
        feed_map = RESERVE_CHAINLINK_ETH_FEEDS if feed_map is None else feed_map
        self._max_staleness = max_staleness
        self._weth_address = weth_address.lower()

        raw = _load_answer_updated(chainlink_dir)

        self._by_address: dict[str, pd.DataFrame] = {}
        self._coverage_end: dict[str, int] = {}
        for reserve, spec in feed_map.items():
            series = _merge_aggregator_series(
                raw,
                spec["aggregators"],
                decimals=spec["decimals"],
                aggregator_eras=spec.get("aggregator_eras"),
            )
            if series.empty:
                continue
            series = series.assign(
                _order_key=_order_key(
                    series["block_number"].to_numpy(), series["log_index"].to_numpy()
                )
            )
            self._by_address[reserve.lower()] = series
            coverage_end = spec.get("coverage_end")
            if coverage_end is not None:
                self._coverage_end[reserve.lower()] = coverage_end

    def price_at(
        self,
        address: str,
        timestamp: pd.Timestamp,
        block_number: int | None = None,
        log_index: int | None = None,
    ) -> float | None:
        """ETH price nearest-prior to `timestamp`; 1.0 for WETH; `None` past
        a coverage gap, at/after a reserve's `coverage_end`, or beyond
        `max_staleness` -- see class docstring.

        `block_number`/`log_index` (CAS-28 H4b): resolve strictly before
        that log position instead of at-or-before `timestamp` alone -- see
        `ChainlinkPriceOracle.price_at`'s docstring for why/when these
        differ.

        `coverage_end` (CAS-28 Lever 11): some reserves' feed map entry
        records a real block at which Aave's own `AssetSourceUpdated`
        history shows it switched away from every mapped aggregator to a
        source this project can't price (e.g. a live-computation wrapper
        with no event history) -- past that block there is genuinely no
        known-correct ETH price, so this returns `None` (a true coverage
        gap, not stale data) rather than silently reusing the last mapped
        aggregator's answer forever under H2's unbounded staleness. Only
        enforced when `block_number` is given -- `coverage_end` is a block
        boundary, not a timestamp one."""
        if not address:
            return None
        address = address.lower()
        if address == self._weth_address:
            return 1.0
        coverage_end = self._coverage_end.get(address)
        if (
            coverage_end is not None
            and block_number is not None
            and block_number >= coverage_end
        ):
            return None
        series = self._by_address.get(address)
        if series is None or series.empty:
            return None
        if block_number is not None and log_index is not None:
            query_key = _order_key(block_number, log_index)
            idx = int(series["_order_key"].searchsorted(query_key, side="right")) - 1
        else:
            idx = (
                int(series["block_timestamp"].searchsorted(timestamp, side="right")) - 1
            )
        if idx < 0:
            return None
        row = series.iloc[idx]
        if timestamp - row["block_timestamp"] > self._max_staleness:
            return None
        return float(row["price"])

    def prices_at(
        self,
        addresses: list[str],
        timestamp: pd.Timestamp,
        block_number: int | None = None,
        log_index: int | None = None,
    ) -> dict[str, float]:
        out = {}
        for address in addresses:
            price = self.price_at(address, timestamp, block_number, log_index)
            if price is not None:
                out[address.lower()] = price
        return out

    def coverage(self) -> set[str]:
        """Reserve addresses with at least one available ETH price point,
        including WETH's identity."""
        return set(self._by_address.keys()) | {self._weth_address}


class PreferEthNumeraireOracle:
    """Per-position oracle preferring native ETH-numeraire pricing, falling
    back to `BlendedPriceOracle` (USD) for the WHOLE position when any single
    leg lacks ETH coverage (CAS-28 H1).

    Numeraires must never mix within one HF ratio -- weighting one leg's
    collateral in ETH and another leg's debt in USD would produce a
    meaningless number. `prices_at` therefore treats its `addresses` argument
    as one atomic position (matching how
    `t2_gate.reconstruct_hf_at_trigger` calls it: once per position, with
    every reserve that position touches) and only returns `EthNumeraire`
    prices if EVERY address in the call has ETH coverage; otherwise it
    returns `BlendedPriceOracle` prices for every address, uncontaminated by
    any partial ETH pricing. This is the T2 gate's current primary oracle --
    on the golden-window subset where both oracles fully cover a position, it
    roughly halves the mismatch rate (21.75% -> 10.81%, see
    `experiments/T2/CAS28_mismatch_next_steps.md`), because WETH-vs-stables
    positions (the dominant case) then depend on the single asset/ETH feed
    Aave actually read instead of an implied cross-rate.

    Degrades gracefully to `BlendedPriceOracle`-only if the Chainlink lake
    hasn't been pulled (`EthNumeraire` raises `FileNotFoundError`), the same
    no-op-when-missing convention as `BlendedPriceOracle` itself.
    """

    def __init__(
        self,
        eth: EthNumeraire | None = None,
        blended: BlendedPriceOracle | None = None,
    ):
        self._blended = BlendedPriceOracle() if blended is None else blended
        if eth is not None:
            self._eth: EthNumeraire | None = eth
        else:
            try:
                self._eth = EthNumeraire()
            except FileNotFoundError:
                self._eth = None

    def price_at(
        self,
        address: str,
        timestamp: pd.Timestamp,
        block_number: int | None = None,
        log_index: int | None = None,
    ) -> float | None:
        """Single-address convenience -- there's only one leg, so there's
        nothing to mix; prefer the ETH price, else fall back to blended."""
        if self._eth is not None:
            price = self._eth.price_at(address, timestamp, block_number, log_index)
            if price is not None:
                return price
        return self._blended.price_at(address, timestamp, block_number, log_index)

    def prices_at(
        self,
        addresses: list[str],
        timestamp: pd.Timestamp,
        block_number: int | None = None,
        log_index: int | None = None,
    ) -> dict[str, float]:
        needed = [a.lower() for a in addresses if a]
        if self._eth is not None and needed:
            eth_prices = self._eth.prices_at(needed, timestamp, block_number, log_index)
            if all(a in eth_prices for a in needed):
                return eth_prices
        return self._blended.prices_at(addresses, timestamp, block_number, log_index)

    def coverage(self) -> set[str]:
        """Union of both sources' covered reserve addresses (NOT a claim
        that any single position gets ETH pricing -- see class docstring)."""
        cov = self._blended.coverage()
        if self._eth is not None:
            cov = cov | self._eth.coverage()
        return cov


_DEFAULT_LIVE_ORACLE_CACHE = Path(
    "data/raw/aave_oracle_live/chain=1/asset_price_cache.parquet"
)
_AAVE_ORACLE_ADDRESS = "0xA50ba011c48153De246E5192C8f9258A2ba79Ca9"
# keccak256("getAssetPrice(address)"), derived via Crypto.Hash.keccak (CAS-28)
# -- verified live: getAssetPrice(WETH) returns exactly 1e18 at every block
# tested, including blocks from Aave v2's first week (matching WETH's role
# as this oracle's own ETH-denominated numeraire, same as EthNumeraire's
# identity special-case below).
_SELECTOR_GET_ASSET_PRICE = "b3596f07"
_ETH_WAD = 10**18


class LiveAaveOracleFallback:
    """Live on-chain price via Aave v2's real `AaveOracle.getAssetPrice(asset)`
    (CAS-28), queried by `eth_call` at the exact historical block on an
    archive RPC -- the same technique `archive_ground_truth_spotcheck.py`
    (H7) uses for `getUserAccountData`. ETH-WAD-scaled (1e18), the same
    numeraire as `EthNumeraire` -- confirmed live, not assumed: querying WETH
    itself returns exactly `1e18` at every block tested.

    Why this exists: `EthNumeraire`/`BlendedPriceOracle` are reconstructions
    from Chainlink `AnswerUpdated` event logs -- they can only be as complete
    as the aggregator addresses this project has mapped and pulled. This
    oracle instead asks Aave's own deployed contract directly, which is
    strictly more authoritative: it reflects whatever source (Chainlink
    aggregator *or* Aave's internal fallback-oracle mechanism) Aave itself
    used at that exact block, including sources this project has never
    discovered. Spot-checked against the "no coverage" residual (CAS-28,
    post-Lever-11c): `getAssetPrice(BUSD)` returns a real, economically sane
    price (~0.00109 ETH, consistent with BUSD's $1 peg at Jan-2021 ETH
    prices) at block 11,587,428 -- 429,336 blocks *before* this project's
    earliest pulled BUSD/USD `AnswerUpdated` (block 12,016,764). Aave's own
    oracle had a real answer here; this project's Chainlink-log
    reconstruction simply hasn't found that source yet.

    Deliberately NOT a bulk-pullable series like every other oracle in this
    module: `getAssetPrice` is a live contract read tied to one exact block,
    not an event log with continuous history, so there is no equivalent of
    "pull once, replay offline forever" -- each distinct (asset, block) pair
    costs one archive `eth_call`. Not used as a blanket replacement for
    `EthNumeraire`/`BlendedPriceOracle` across the whole ~49k-liquidation
    population -- see `t2_gate.apply_live_oracle_fallback`, which scopes it
    to just the `unexplained` mismatch bucket, where a full-population
    check (CAS-28, n=2,896, not a sample) measured a 58.6% correction rate
    (2.6% at the actual T2 gate, down from 6.05%) while agreeing closely
    with the existing reconstruction on an equal-sized already-matching
    sample (median 0.22% price diff) -- the win is concentrated in the
    residual, not a signal to replace Chainlink everywhere. Caches every
    (address, block) result to `cache_path` (default
    `data/raw/aave_oracle_live/chain=1/`, a regular tracked/LFS'd dataset
    like every other source in this project, not a gitignored checkpoint --
    unlike a resumable-backfill checkpoint, this cache is meant to be reused
    across sessions/machines, not just to survive one interrupted run) so a
    repeated query or a re-run after a code change never re-pays for the
    same call.

    `ARCHIVE_RPC_URL` is only required lazily, on the first cache-miss fetch
    -- a cache built by an earlier run degrades gracefully offline (no env
    var, no network) the same way `PreferEthNumeraireOracle` degrades to
    `BlendedPriceOracle`-only when the Chainlink lake hasn't been pulled:
    every already-cached (address, block) still resolves; a genuinely new
    pair simply returns `None` (no correction available) instead of raising.
    """

    def __init__(
        self,
        rpc_url: str | None = None,
        cache_path: str | Path = _DEFAULT_LIVE_ORACLE_CACHE,
        weth_address: str = WETH_ADDRESS,
    ):
        self._rpc_url = rpc_url
        self._weth_address = weth_address.lower()
        self._cache_path = Path(cache_path)
        self._cache: dict[str, float | None] = {}
        if self._cache_path.exists():
            cached = pd.read_parquet(self._cache_path)
            addresses = cached["address"].astype(str).to_numpy()
            blocks = cached["block_number"].astype(int).to_numpy()
            prices = cached["price"].to_numpy()
            for address, block_number, price in zip(addresses, blocks, prices):
                key = self._cache_key(address, int(block_number))
                self._cache[key] = None if pd.isna(price) else float(price)
        self.n_live_calls = 0

    @staticmethod
    def _cache_key(address: str, block_number: int) -> str:
        return f"{address.lower()}_{block_number}"

    def price_at(
        self,
        address: str,
        timestamp: pd.Timestamp,
        block_number: int | None = None,
        log_index: int | None = None,
    ) -> float | None:
        """`getAssetPrice(address)` at `block_number` -- `None` if
        `block_number` isn't given (this oracle has no timestamp-only
        resolution, unlike the log-based oracles elsewhere in this module;
        a live call must be pinned to one exact block) or the call reverts
        (genuinely no source configured, including no fallback oracle)."""
        if not address or block_number is None:
            return None
        address = address.lower()
        if address == self._weth_address:
            return 1.0
        key = self._cache_key(address, block_number)
        if key in self._cache:
            return self._cache[key]
        price = self._fetch_live(address, block_number)
        self._cache[key] = price
        self._save_cache()
        return price

    def prices_at(
        self,
        addresses: list[str],
        timestamp: pd.Timestamp,
        block_number: int | None = None,
        log_index: int | None = None,
    ) -> dict[str, float]:
        out = {}
        for address in addresses:
            price = self.price_at(address, timestamp, block_number, log_index)
            if price is not None:
                out[address.lower()] = price
        return out

    def _fetch_live(
        self, address: str, block_number: int, retries: int = 4
    ) -> float | None:
        rpc_url = self._rpc_url or os.environ.get("ARCHIVE_RPC_URL")
        if not rpc_url:
            return None
        calldata = "0x" + _SELECTOR_GET_ASSET_PRICE + address[2:].rjust(64, "0").lower()
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [
                {"to": _AAVE_ORACLE_ADDRESS, "data": calldata},
                hex(block_number),
            ],
            "id": 1,
        }
        result: dict = {}
        for attempt in range(retries):
            try:
                resp = requests.post(rpc_url, json=payload, timeout=20)
                result = resp.json()
            except (requests.RequestException, ValueError):
                time.sleep(1.5 * (attempt + 1))
                continue
            self.n_live_calls += 1
            if "error" in result:
                # A revert (no source and no fallback oracle configured) is a
                # real "no price" answer, not a transient failure -- don't
                # retry it away. Matched narrowly on the standard EVM
                # "execution reverted" phrase (CAS-28: an earlier, looser
                # `"revert" in message or "execution" in message` check
                # mis-cached at least one confirmed-real price as a
                # permanent None -- BUSD@11,587,428 resolved to 0.001085 ETH
                # on direct re-query, not a revert -- almost certainly a
                # rate-limit/timeout message that happened to contain
                # "execution" get misread as a contract revert).
                message = str(result["error"]).lower()
                if "execution reverted" in message:
                    return None
                time.sleep(1.5 * (attempt + 1))
                continue
            raw = int(result["result"], 16)
            return raw / _ETH_WAD if raw > 0 else None
        raise RuntimeError(
            f"getAssetPrice eth_call failed after {retries} retries: {result}"
        )

    def _save_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for key, price in self._cache.items():
            address, block_str = key.rsplit("_", 1)
            rows.append(
                {"address": address, "block_number": int(block_str), "price": price}
            )
        pd.DataFrame(rows).to_parquet(self._cache_path, index=False)
