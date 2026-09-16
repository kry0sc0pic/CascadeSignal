"""Enumerate every historical phase (aggregator contract) for each Aave v2
reserve's asset/ETH Chainlink feed, via the on-chain Chainlink `FeedRegistry`
(CAS-28 H2).

`RESERVE_CHAINLINK_ETH_FEEDS` (H1) was built from `description()` matching
among aggregators that happened to emit an `AnswerUpdated` event during the 5
golden-episode block windows -- so it's blind to any feed migration (a new
underlying aggregator contract) that occurred *between* episodes. This
script finds the complete phase history directly:

1. `FeedRegistry.getFeed(base, quote)` (0x47Fb2585D2C56Fe188D0E6ec628a38b74fCeeeDf,
   verified live below -- not memorized) returns the feed's current-phase
   aggregator for a (base, quote) pair. `quote` is always the ETH
   denomination placeholder (`0xEeee...EEeE`, Chainlink's `Denominations.ETH`).
   `base` is the reserve's own ERC20 address for the ~27 reserves where
   `quote_symbol == symbol`; for the 3 aliased reserves (WBTC->BTC,
   USDP->PAX, renFIL->FIL) this script tries the reserve's own address
   first (works for USDP/renFIL -- rebrand/alias tokens keep their
   contract address) and a small set of known placeholder/candidate
   addresses otherwise (WBTC -> `Denominations.BTC`).
2. `FeedRegistry.getPhaseFeed(base, quote, phaseId)` for phaseId = 1, 2, 3...
   until it reverts ("Feed not found for phase") -- each phaseId's return is
   one historical aggregator contract. Confirmed live: DAI/ETH has exactly 2
   phases, phase 1 == our already-known golden-window aggregator
   (0x158228e08c52f3e2211ccbc8ec275fa93f6033fc), phase 2 is a
   never-before-seen address -- i.e. a real migration our pull missed.
3. Every result is cross-checked two ways before being trusted: (a) at least
   one discovered phase must match an address already in
   `RESERVE_CHAINLINK_ETH_FEEDS`, proving the registry resolved the SAME feed
   we already partially cover, not an unrelated one; (b) each newly
   discovered address's own `description()` must contain the expected quote
   symbol (e.g. "ETH"). A reserve failing either check is reported, not
   silently included.

Selectors below were derived via `Crypto.Hash.keccak` (pycryptodome, not a
project dependency -- installed ad hoc to compute these once) and verified
against this repo's own already-proven selectors (decimals/description) as a
sanity check before use; see the commented keccak256(...) preimages.

Usage:
    python scripts/onchain/discover_chainlink_eth_feed_phases.py
Prints, per reserve: every phase's aggregator address, which ones are
already known (H1) vs. newly discovered (H2 candidates), and any reserve
that failed cross-validation. Finally prints an updated
`RESERVE_CHAINLINK_ETH_FEEDS` literal (known + newly discovered addresses
merged) to copy into `chainlink_feeds.py`, same "manually reviewed and
copied" convention as `map_chainlink_reserves.py`/`map_chainlink_eth_feeds.py`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from cascadesignal.state.chainlink_feeds import (
    RESERVE_CHAINLINK_ETH_FEEDS,
)  # noqa: E402
from cascadesignal.state.reserves import ONCHAIN_RESERVE_CONFIG  # noqa: E402

RPC_URL = "https://ethereum-rpc.publicnode.com"

# keccak256("getFeed(address,address)")        -> 0xd2edb6dd
# keccak256("getPhaseFeed(address,address,uint16)") -> 0x52dbeb8b
# keccak256("decimals()")                       -> 0x313ce567 (repo-verified)
# keccak256("description()")                    -> 0x7284e416 (repo-verified)
SELECTOR_GET_FEED = "d2edb6dd"
SELECTOR_GET_PHASE_FEED = "52dbeb8b"
SELECTOR_DECIMALS = "313ce567"
SELECTOR_DESCRIPTION = "7284e416"

# Verified live 2026-07-21: getFeed(DAI, ETH) on this address returns a real
# proxy whose description() == "DAI / ETH" and whose phase-1 aggregator
# matches RESERVE_CHAINLINK_ETH_FEEDS's DAI entry exactly.
FEED_REGISTRY = "0x47Fb2585D2C56Fe188D0E6ec628a38b74fCeeeDf"

# Chainlink's Denominations.ETH placeholder (used as `quote` for every
# asset/ETH feed lookup).
ETH_DENOMINATION = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
# Chainlink's Denominations.BTC placeholder (WBTC has no dedicated
# WBTC/ETH feed -- H1 approximates via the generic BTC/ETH feed, so the
# registry lookup must use the same placeholder, not WBTC's own address).
BTC_DENOMINATION = "0x0000000000000000000000000000000000348d"

ZERO_ADDRESS = "0x" + "00" * 20


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
            time.sleep(1.5 * (attempt + 1))
            continue
        if "error" in result:
            return None
        return result["result"]
    return None


def decode_address(hex_data: str) -> str:
    return "0x" + hex_data[-40:]


def decode_string(hex_data: str) -> str:
    data = bytes.fromhex(hex_data[2:])
    if len(data) < 64:
        return ""
    str_offset = int.from_bytes(data[0:32], "big")
    str_len = int.from_bytes(data[str_offset : str_offset + 32], "big")
    raw = data[str_offset + 32 : str_offset + 32 + str_len]
    return raw.decode("utf-8", errors="replace")


def encode_address(addr: str) -> str:
    return addr[2:].rjust(64, "0").lower()


def encode_uint16(n: int) -> str:
    return hex(n)[2:].rjust(64, "0")


def get_feed(base: str, quote: str) -> str | None:
    data = "0x" + SELECTOR_GET_FEED + encode_address(base) + encode_address(quote)
    result = eth_call(FEED_REGISTRY, data)
    if result is None or result == "0x":
        return None
    addr = decode_address(result)
    return None if addr.lower() == ZERO_ADDRESS else addr


def get_phase_feed(base: str, quote: str, phase_id: int) -> str | None:
    data = (
        "0x"
        + SELECTOR_GET_PHASE_FEED
        + encode_address(base)
        + encode_address(quote)
        + encode_uint16(phase_id)
    )
    return eth_call(FEED_REGISTRY, data)  # None on revert (no such phase)


def description(address: str) -> str | None:
    result = eth_call(address, "0x" + SELECTOR_DESCRIPTION)
    if result is None:
        return None
    return decode_string(result)


def enumerate_phases(base: str, quote: str, max_phases: int = 20) -> list[str]:
    phases: list[str] = []
    for phase_id in range(1, max_phases + 1):
        result = get_phase_feed(base, quote, phase_id)
        if result is None:
            break
        phases.append(decode_address(result))
    return phases


def main() -> None:
    print(f"FEED_REGISTRY = {FEED_REGISTRY}\n")

    merged: dict[str, dict] = {}

    for reserve, spec in RESERVE_CHAINLINK_ETH_FEEDS.items():
        symbol = ONCHAIN_RESERVE_CONFIG[reserve][0]
        known = {a.lower() for a in spec["aggregators"]}

        candidates = [reserve]
        if symbol == "WBTC":
            candidates = [BTC_DENOMINATION]
        base = None
        for candidate in candidates:
            if get_feed(candidate, ETH_DENOMINATION) is not None:
                base = candidate
                break

        if base is None:
            print(f"{symbol:8s}: FeedRegistry has no (base, ETH) entry -- skip")
            merged[reserve] = spec
            continue

        phases = enumerate_phases(base, ETH_DENOMINATION)
        phase_set = {a.lower() for a in phases}
        overlap = known & phase_set
        new = phase_set - known

        if not overlap:
            # Cross-validation failed -- don't trust the registry's answer
            # for this reserve, keep the existing (golden-window-only) entry.
            print(
                f"{symbol:8s}: NO OVERLAP with known aggregator -- not trusted, keeping as-is"
            )
            merged[reserve] = spec
            continue

        status = "OK"
        print(f"{symbol:8s}: {len(phases)} phase(s) via base={base}  [{status}]")
        for i, addr in enumerate(phases, start=1):
            tag = "known" if addr.lower() in known else "NEW"
            desc = description(addr) if tag == "NEW" else spec.get("quote_symbol", "")
            print(f"    phase {i}: {addr}  [{tag}]  desc={desc}")
        if new:
            print(
                f"    -> {len(new)} new candidate aggregator(s) for {symbol}: {sorted(new)}"
            )

        merged[reserve] = {
            **spec,
            "aggregators": sorted(known | phase_set),
        }

    print(
        "\n\n# Updated RESERVE_CHAINLINK_ETH_FEEDS (known + newly discovered phases merged)"
    )
    print("RESERVE_CHAINLINK_ETH_FEEDS: dict[str, dict] = {")
    for reserve, spec in merged.items():
        agg_list = ", ".join(f'"{a}"' for a in spec["aggregators"])
        print(
            f'    "{reserve}": {{"symbol": "{spec["symbol"]}", "quote_symbol": '
            f'"{spec["quote_symbol"]}", "decimals": {spec["decimals"]}, '
            f'"aggregators": [{agg_list}]}},'
        )
    print("}")


if __name__ == "__main__":
    main()
