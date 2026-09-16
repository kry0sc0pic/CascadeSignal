"""Validate `RESERVE_CHAINLINK_ETH_FEEDS` against Aave's own historical oracle
sources, per era (CAS-28, Lever 11a).

`RESERVE_CHAINLINK_ETH_FEEDS` (H1/H2) was built by description-matching
aggregators discovered via the 5 golden-episode windows, then expanded via
the Chainlink `FeedRegistry`'s `getPhaseFeed` -- but H2's own docstring
records that 8 reserves (WBTC, BUSD, TUSD, renFIL, USDP, DPI, FRAX, FEI,
UST) have **no FeedRegistry entry** for their (base, quote) pair at all, so
those 8 kept whatever single aggregator H1's golden-window sweep happened to
find, with no cross-check that it's complete.

This script sidesteps the FeedRegistry gap entirely by using
`data/raw/aave_v2_oracle_sources/chain=1/asset_source_updated.parquet`
(`backfill_aave_oracle_sources.py`, CAS-28 Lever 11) -- Aave's own
`AaveOracle.getSourceOfAsset` history via `AssetSourceUpdated`. Each
historical `source` Aave actually pointed to is walked *directly*:

1. If `source` responds to `decimals()`/`description()` (a plain Chainlink
   aggregator or proxy), it's a real, usable feed.
2. If `source` additionally responds to `phaseId()`/`phaseAggregators(n)`
   (the standard `EACAggregatorProxy` pattern -- confirmed live on Aave's
   own WBTC/ETH source, `0xdeb288f7...`, which has 5 phases; phase 4 is
   `RESERVE_CHAINLINK_ETH_FEEDS`'s current WBTC entry, phase 5 is the
   CURRENT one at the time this script ran -- our map is one phase stale)
   the FULL phase history is walked directly off that proxy, no
   FeedRegistry lookup needed at all -- this is what recovers the 8
   FeedRegistry-blind reserves.
3. If `source` reverts on `decimals()`/`description()` entirely, it's a
   genuinely custom adapter (confirmed for GUSD's 2 sources, xSUSHI's
   original 2021 source, ENS, and LUSD's original 2022 source -- none of
   these emit a standard `AnswerUpdated` either, so no amount of address
   discovery recovers them; see Lever 11b for the `getsourcecode` path).

Reports, per reserve: Aave's own source-address eras (from
`AssetSourceUpdated`), each era's resolved phase-aggregator set, and a diff
against `RESERVE_CHAINLINK_ETH_FEEDS`'s current `aggregators` list -- any
phase Aave used that our map doesn't have is a real coverage gap, not a
hypothetical one.

Usage:
    python scripts/onchain/validate_aave_oracle_sources_per_era.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from cascadesignal.state.chainlink_feeds import (  # noqa: E402
    RESERVE_CHAINLINK_ETH_FEEDS,
    UNCOVERED_ETH_RESERVES,
)
from cascadesignal.state.reserves import ONCHAIN_RESERVE_CONFIG  # noqa: E402

RPC_URL = "https://ethereum-rpc.publicnode.com"

# All derived via Crypto.Hash.keccak and verified live this session (see
# CAS28_mismatch_next_steps.md's Lever 11 section for the specific hits).
SELECTOR_DECIMALS = "313ce567"
SELECTOR_DESCRIPTION = "7284e416"
SELECTOR_PHASE_ID = "58303b10"  # phaseId()
SELECTOR_PHASE_AGGREGATORS = "c1597304"  # phaseAggregators(uint16)

_SOURCES_PARQUET = Path(
    "data/raw/aave_v2_oracle_sources/chain=1/asset_source_updated.parquet"
)

_KNOWN_CUSTOM_NO_EVENT_SOURCES = {
    # Confirmed this session: decimals()/description() revert AND no
    # AnswerUpdated log exists at all -- genuinely custom, no amount of
    # address discovery recovers these, see Lever 11b.
    "0x61322e7eb0853efdecdb0570f6d0870a41a689c5",  # GUSD era 1
    "0xec6f4cd64d28ef32507e2dc399948aae9bbedd7e",  # GUSD era 2
    "0x9b26214bec078e68a394aaebfbfff406ce14893f",  # xSUSHI era 1
    "0xd4641b75015e6536e8102d98479568d05d7123db",  # ENS
    "0x60c0b047133f696334a2b7f68af0b49d2f3d4f72",  # LUSD era 1
}


def eth_call(to: str, data: str, retries: int = 4) -> str | None:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
        "id": 1,
    }
    for attempt in range(retries):
        try:
            resp = requests.post(RPC_URL, json=payload, timeout=20)
            result = resp.json()
        except requests.RequestException:
            time.sleep(1.0 * (attempt + 1))
            continue
        if "error" in result:
            return None
        return result["result"]
    return None


def decode_address(hex_data: str | None) -> str | None:
    if not hex_data or hex_data == "0x":
        return None
    return "0x" + hex_data[-40:]


def decode_uint(hex_data: str | None) -> int | None:
    return int(hex_data, 16) if hex_data and hex_data != "0x" else None


def decode_string(hex_data: str | None) -> str | None:
    if not hex_data or hex_data == "0x":
        return None
    data = bytes.fromhex(hex_data[2:])
    if len(data) < 64:
        return None
    str_offset = int.from_bytes(data[0:32], "big")
    str_len = int.from_bytes(data[str_offset : str_offset + 32], "big")
    raw = data[str_offset + 32 : str_offset + 32 + str_len]
    return raw.decode("utf-8", errors="replace")


def resolve_phases(source: str, max_phases: int = 20) -> list[str] | None:
    """Full phase-aggregator history directly off `source` (no FeedRegistry
    needed) -- `None` if `source` isn't a phased proxy at all (either a
    plain non-proxy aggregator, or a custom adapter)."""
    phase_id_hex = eth_call(source, "0x" + SELECTOR_PHASE_ID)
    current_phase = decode_uint(phase_id_hex)
    if current_phase is None or current_phase == 0:
        return None
    phases = []
    for phase_id in range(1, min(current_phase, max_phases) + 1):
        arg = hex(phase_id)[2:].rjust(64, "0")
        result = eth_call(source, "0x" + SELECTOR_PHASE_AGGREGATORS + arg)
        addr = decode_address(result)
        if addr is None or addr == "0x" + "00" * 20:
            continue
        phases.append(addr.lower())
    return phases or None


def classify(source: str) -> dict:
    if source in _KNOWN_CUSTOM_NO_EVENT_SOURCES:
        return {"kind": "custom_no_event", "phases": []}
    decimals = decode_uint(eth_call(source, "0x" + SELECTOR_DECIMALS))
    if decimals is None:
        return {"kind": "unresolved_revert", "phases": []}
    phases = resolve_phases(source)
    if phases:
        return {"kind": "proxy", "phases": phases}
    return {"kind": "plain_aggregator", "phases": [source.lower()]}


def main() -> None:
    sources_df = pd.read_parquet(_SOURCES_PARQUET)
    sym_by_addr = {addr.lower(): cfg[0] for addr, cfg in ONCHAIN_RESERVE_CONFIG.items()}
    sources_df["symbol"] = sources_df["asset"].map(sym_by_addr)
    reserve_sources = sources_df[sources_df["symbol"].notna()].sort_values(
        ["asset", "block_number"]
    )

    gaps: list[dict] = []
    for reserve_addr_raw, group in reserve_sources.groupby("asset"):
        reserve_addr = str(reserve_addr_raw)
        symbol = group["symbol"].iloc[0]
        current_spec = RESERVE_CHAINLINK_ETH_FEEDS.get(reserve_addr)
        known = (
            {a.lower() for a in current_spec["aggregators"]} if current_spec else set()
        )
        uncovered = reserve_addr in UNCOVERED_ETH_RESERVES

        print(
            f"\n{symbol} ({reserve_addr}) -- {'UNCOVERED' if uncovered else 'covered'}:"
        )
        ground_truth: set[str] = set()
        for _, row in group.iterrows():
            source = row["source"]
            result = classify(source)
            ground_truth.update(result["phases"])
            phases_str = ", ".join(result["phases"]) or "(none resolved)"
            print(
                f"  era from block {int(row['block_number'])} "
                f"({row['block_timestamp'].date()}): source={source} "
                f"kind={result['kind']} phases=[{phases_str}]"
            )

        missing = ground_truth - known
        extra = known - ground_truth
        if missing:
            print(f"  -> MISSING from map: {sorted(missing)}")
            gaps.append(
                {"symbol": symbol, "reserve": reserve_addr, "missing": sorted(missing)}
            )
        if extra:
            print(f"  -> in map but not in Aave's own source history: {sorted(extra)}")
        if not missing and not extra and ground_truth:
            print("  -> map matches Aave's full resolved source history exactly")
        if not ground_truth:
            print(
                "  -> no phase/aggregator resolved at all (fully custom, see Lever 11b)"
            )

    print(f"\n\n{len(gaps)} reserve(s) with a confirmed real coverage gap:")
    for gap in gaps:
        print(f"  {gap['symbol']}: missing {gap['missing']}")


if __name__ == "__main__":
    main()
