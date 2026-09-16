"""Derive synthetic `AnswerUpdated` history for GUSD/ENS/LUSD's custom Aave
oracle-source adapters (CAS-28, Lever 11b), from the raw underlying feeds
`backfill_custom_adapter_underlying_feeds.py` pulled.

Each adapter computes its answer *fresh on every call* from 1-2 live
Chainlink feeds (see that script's docstring for the exact formulas,
confirmed via `getsourcecode`) -- there's no on-chain event history for the
adapter itself. To get a nearest-prior-lookup series that behaves exactly
like a real feed (so it slots into `RESERVE_CHAINLINK_ETH_FEEDS` and
`EthNumeraire`/`_merge_aggregator_series` with zero changes to `prices.py`),
this script materializes one row at *every* block where either underlying
input changes -- the union of both feeds' breakpoints, not just one side's.

This is exact, not an approximation: between any two consecutive breakpoints
neither input has moved, so the ratio is constant there, and "nearest prior
row in the synthetic series" reproduces exactly what a fresh on-chain call
would return at any query point in between. (Materializing only one side's
breakpoints -- e.g. `ChainlinkPriceOracle._convert_via_eth_usd`'s existing
one-directional USD/ETH conversion for BAL/USDP -- would miss the case where
the *other* feed moved more recently than the materialized side, understating
freshness. Not fixed here since it's a separate, smaller, already-accepted
approximation elsewhere in this codebase; this script's 2-directional
approach is only needed because GUSD/ENS/LUSD's replication has no adapter
event history to fall back on at all.)

All arithmetic uses plain Python ints, matching Solidity's own integer
(floor) division exactly -- no float rounding.

Usage:
    python scripts/onchain/derive_custom_adapter_eth_feeds.py
Writes `data/raw/chainlink/chain=1/derived_custom_adapter_eth_feeds.parquet`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from cascadesignal.ingest.schema import normalize, to_arrow  # noqa: E402

_IN_PARQUET = Path(
    "data/raw/chainlink/chain=1/custom_adapter_underlying_answer_updated.parquet"
)
_OUT_PARQUET = Path(
    "data/raw/chainlink/chain=1/derived_custom_adapter_eth_feeds.parquet"
)

_ORDER_KEY_LOG_INDEX_MULTIPLIER = 1_000_000  # matches prices._order_key

_ETH_USD_ADDRESSES = [
    "0xf79d6afbb6da890132f9d7c355e3015f15f3406f",
    "0xb103ede8acd6f0c106b7a5772e9d24e34f5ebc2c",
    "0x00c7a37b03690fb9f41b5c5af8131735c7275446",
    "0xd3fcd40153e56110e6eeae13e12530e26c9cb4fd",
    "0x37bc7498f4ff12c19678ee8fe19d713b87f6a9e6",
    "0xe62b71cf983019bff55bc83b48601ce8419650cc",
    "0x7d4e742018fb52e48b08be73d041c18b21de6fb5",
]
_ENS_USD_ADDRESSES = [
    "0x780f1bd91a5a22ede36d4b2b2c0eccb9b1726a28",
    "0x6cc5173ffd8d674c64f2dc7237730ff021829865",
]
_LUSD_USD_ADDRESSES = [
    "0x27b97a63091d185ce056e1747624b9b92baad056",
]

# Real wrapper contract addresses (Aave's own `AssetSourceUpdated` history) --
# used as the synthetic `user` (aggregator) key so these slot straight into
# `RESERVE_CHAINLINK_ETH_FEEDS.aggregators` like a real feed.
_GUSD_WRAPPER = "0x61322e7eb0853efdecdb0570f6d0870a41a689c5"
_ENS_WRAPPER = "0xd4641b75015e6536e8102d98479568d05d7123db"
_LUSD_WRAPPER = "0x60c0b047133f696334a2b7f68af0b49d2f3d4f72"

# LUSD's original-source era ends here (Aave's real 2024-04-24 migration to a
# new, also-unmapped dead-wrapper source, confirmed in Lever 11 finding 3) --
# clip so a stale post-migration answer can't bleed forward under H2's
# unbounded staleness. Applied via `coverage_end` in chainlink_feeds.py, not
# here; this script just doesn't need data past that block anyway.


def _merge_series(raw: pd.DataFrame, addresses: list[str]) -> pd.DataFrame:
    """Sorted, deduped (block_number, log_index) series for one logical feed,
    same convention as `prices._merge_aggregator_series`."""
    sub = raw[raw["user"].isin([a.lower() for a in addresses])]
    out = sub[["block_number", "log_index", "block_timestamp", "tx_hash", "value"]]
    return (
        out.sort_values(["block_number", "log_index"])
        .drop_duplicates(subset=["block_number", "log_index"], keep="last")
        .reset_index(drop=True)
    )


def _order_key(block_number: np.ndarray, log_index: np.ndarray) -> np.ndarray:
    return block_number.astype(
        np.int64
    ) * _ORDER_KEY_LOG_INDEX_MULTIPLIER + log_index.astype(np.int64)


def _nearest_prior_value(
    series: pd.DataFrame, order_keys: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """`series["value"]` at the last row with order_key <= each query key (0 if none)."""
    series_keys = _order_key(
        series["block_number"].to_numpy(), series["log_index"].to_numpy()
    )
    idx = np.searchsorted(series_keys, order_keys, side="right") - 1
    values = series["value"].to_numpy()
    out = np.zeros(len(order_keys), dtype=object)
    valid = idx >= 0
    out[valid] = values[idx[valid]]
    return out, valid


def _combine_two_feeds(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    """Union of both feeds' breakpoints, each paired with the OTHER feed's
    nearest-prior-or-equal value -- see module docstring for why this exact
    reproduces a fresh two-feed on-chain read at any query point."""
    a_keys = _order_key(a["block_number"].to_numpy(), a["log_index"].to_numpy())
    b_keys = _order_key(b["block_number"].to_numpy(), b["log_index"].to_numpy())

    b_at_a, b_at_a_valid = _nearest_prior_value(b, a_keys)
    a_at_b, a_at_b_valid = _nearest_prior_value(a, b_keys)

    rows = []
    for i in range(len(a)):
        if not b_at_a_valid[i]:
            continue
        rows.append(
            {
                "block_number": int(a["block_number"].iloc[i]),
                "log_index": int(a["log_index"].iloc[i]),
                "block_timestamp": a["block_timestamp"].iloc[i],
                "tx_hash": a["tx_hash"].iloc[i],
                "asset_usd": int(a["value"].iloc[i]),
                "eth_usd": int(b_at_a[i]),
            }
        )
    for i in range(len(b)):
        if not a_at_b_valid[i]:
            continue
        rows.append(
            {
                "block_number": int(b["block_number"].iloc[i]),
                "log_index": int(b["log_index"].iloc[i]),
                "block_timestamp": b["block_timestamp"].iloc[i],
                "tx_hash": b["tx_hash"].iloc[i],
                "asset_usd": int(a_at_b[i]),
                "eth_usd": int(b["value"].iloc[i]),
            }
        )
    combined = pd.DataFrame(rows)
    combined = combined.sort_values(["block_number", "log_index"]).drop_duplicates(
        subset=["block_number", "log_index"], keep="last"
    )
    return combined.reset_index(drop=True)


def _rows_for_gusd(eth_usd: pd.DataFrame) -> list[dict]:
    """`(1e8 * 1 ether) / ETH_USD.latestAnswer()` at every ETH/USD breakpoint
    (GUSD's only input -- its peg to 1 USD is hard-coded, no GUSD/USD feed
    read at all)."""
    normalization = 10**8 * 10**18
    rows = []
    for _, row in eth_usd.iterrows():
        eth_usd_raw = int(row["value"])
        if eth_usd_raw <= 0:
            continue
        answer = normalization // eth_usd_raw
        rows.append(
            {
                "chain_id": 1,
                "block_number": int(row["block_number"]),
                "block_timestamp": row["block_timestamp"],
                "tx_hash": row["tx_hash"],
                "log_index": int(row["log_index"]),
                "protocol": "chainlink",
                "event_type": "AnswerUpdated",
                "user": _GUSD_WRAPPER,
                "collateral_asset": None,
                "debt_asset": None,
                "amount_raw": str(answer),
                "amount_usd": answer / 1e18,
                "liquidator": None,
                "collateral_seized_raw": None,
                "collateral_seized_usd": None,
            }
        )
    return rows


def _rows_for_cross_rate(
    asset_usd: pd.DataFrame, eth_usd: pd.DataFrame, wrapper: str
) -> list[dict]:
    """`(ASSET_USD.latestAnswer() * 1 ether) / ETH_USD.latestAnswer()` at
    every breakpoint of either feed (ENS/LUSD's shared formula shape)."""
    combined = _combine_two_feeds(asset_usd, eth_usd)
    rows = []
    for _, row in combined.iterrows():
        eth_usd_raw = row["eth_usd"]
        asset_usd_raw = row["asset_usd"]
        if eth_usd_raw <= 0 or asset_usd_raw <= 0:
            continue
        answer = (asset_usd_raw * 10**18) // eth_usd_raw
        rows.append(
            {
                "chain_id": 1,
                "block_number": int(row["block_number"]),
                "block_timestamp": row["block_timestamp"],
                "tx_hash": row["tx_hash"],
                "log_index": int(row["log_index"]),
                "protocol": "chainlink",
                "event_type": "AnswerUpdated",
                "user": wrapper,
                "collateral_asset": None,
                "debt_asset": None,
                "amount_raw": str(answer),
                "amount_usd": answer / 1e18,
                "liquidator": None,
                "collateral_seized_raw": None,
                "collateral_seized_usd": None,
            }
        )
    return rows


def main() -> None:
    raw = pd.read_parquet(_IN_PARQUET)
    raw = raw[raw["event_type"] == "AnswerUpdated"].copy()
    raw["user"] = raw["user"].str.lower()
    raw["value"] = pd.to_numeric(raw["amount_raw"], errors="coerce")
    raw = raw.dropna(subset=["value"])
    raw["value"] = raw["value"].astype("int64")

    eth_usd = _merge_series(raw, _ETH_USD_ADDRESSES)
    ens_usd = _merge_series(raw, _ENS_USD_ADDRESSES)
    lusd_usd = _merge_series(raw, _LUSD_USD_ADDRESSES)
    print(
        f"ETH/USD: {len(eth_usd)} rows, blocks [{eth_usd['block_number'].min()}, "
        f"{eth_usd['block_number'].max()}]"
    )
    print(
        f"ENS/USD: {len(ens_usd)} rows, blocks [{ens_usd['block_number'].min()}, "
        f"{ens_usd['block_number'].max()}]"
    )
    print(
        f"LUSD/USD-p1: {len(lusd_usd)} rows, blocks [{lusd_usd['block_number'].min()}, "
        f"{lusd_usd['block_number'].max()}]"
    )

    all_rows: list[dict] = []
    all_rows.extend(_rows_for_gusd(eth_usd))
    print(f"GUSD: {len(all_rows)} synthetic rows")
    n_before = len(all_rows)
    all_rows.extend(_rows_for_cross_rate(ens_usd, eth_usd, _ENS_WRAPPER))
    print(f"ENS: {len(all_rows) - n_before} synthetic rows")
    n_before = len(all_rows)
    all_rows.extend(_rows_for_cross_rate(lusd_usd, eth_usd, _LUSD_WRAPPER))
    print(f"LUSD: {len(all_rows) - n_before} synthetic rows")

    df = normalize(pd.DataFrame(all_rows), protocol="chainlink")
    df = df.drop_duplicates(subset=["user", "block_number", "log_index"])
    _OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    to_arrow(df).to_pandas().to_parquet(_OUT_PARQUET, index=False)
    print(f"\nWrote {len(df)} rows to {_OUT_PARQUET}")


if __name__ == "__main__":
    main()
