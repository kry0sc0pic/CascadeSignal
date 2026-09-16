"""Build the reserve -> Chainlink feed map baked into `chainlink_feeds.py` (CAS-17/CAS-47).

Combines the two building blocks from the other `scripts/onchain/*.py` in
this directory into the actual symbol-matched mapping used by
`cascadesignal.state.prices.ChainlinkPriceOracle`:

1. `fetch_aave_v2_price_oracle_sources.py` established that Aave v2's
   *current* on-chain `getSourceOfAsset()` mapping is a dead end for
   historical pricing -- none of the 37 modern "Capped X/USD/ETH" wrapper
   addresses it returns appear anywhere in the 423,848-row golden-episode
   `AnswerUpdated` pull (they didn't exist yet during China'21/Terra'22).
2. `describe_chainlink_aggregators.py` instead fetches `description()` +
   `decimals()` for the 392 raw aggregator addresses that actually emitted
   events in the pulled data.

This script matches each of the 37 Aave v2 reserves (by `reserves.py`'s
`ONCHAIN_RESERVE_CONFIG` symbols) against that description index, preferring
a direct `<SYMBOL> / USD` feed and falling back to `<SYMBOL> / ETH` (which
needs converting via the ETH/USD feed) where no USD-quoted feed was pulled.
Chainlink migrates feeds to new proxy addresses periodically, so a single
logical feed's 2021-2026 history is often split across 2-4 successive
contract addresses sharing the same `description()` -- all matching
addresses are kept so the price oracle can merge them into one continuous
series.

Six reserves have no match at all (no raw feed among the 392 pulled
aggregators emitted a single `AnswerUpdated` event across any of the 5
golden-episode block windows): GUSD, xSUSHI, stETH, ENS, CVX genuinely have
no coverage; WBTC has no direct `WBTC/USD` or `WBTC/BTC` feed in the pull, so
it is approximated via the raw `BTC/USD` feed (documented limitation --
ignores the WBTC/BTC peg deviation, which is usually small but not zero
during stress). Whether this reflects the underlying feeds never firing in
those specific windows or Dune's `chainlink_ethereum` decoded-contract
coverage not including them is not established here.

This script's output was manually reviewed and copied into
`chainlink_feeds.py` (not run automatically at import time, since it needs
the pulled parquet + a live RPC and takes a couple minutes) -- re-run it if
the Chainlink pull is ever extended to new block ranges or new episodes.

Usage:
    python scripts/onchain/map_chainlink_reserves.py
Prints the `RESERVE_CHAINLINK_FEEDS` / `UNCOVERED_RESERVES` /
`ETH_USD_AGGREGATORS` Python literals to stdout.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from cascadesignal.state.reserves import ONCHAIN_RESERVE_CONFIG  # noqa: E402

_HERE = Path(__file__).resolve().parent

# symbol -> (quote_symbol, quote_currency) preference. `None` means no raw
# Chainlink feed was found among the pulled aggregators for any alias tried.
SYMBOL_ALIAS: dict[str, tuple[str, str] | None] = {
    "USDT": ("USDT", "USD"),
    "WBTC": ("BTC", "USD"),  # approximation: raw BTC/USD, no WBTC/BTC feed pulled
    "WETH": ("ETH", "USD"),
    "YFI": ("YFI", "USD"),
    "ZRX": ("ZRX", "USD"),
    "UNI": ("UNI", "USD"),
    "AAVE": ("AAVE", "USD"),
    "BAT": ("BAT", "USD"),
    "BUSD": ("BUSD", "USD"),
    "DAI": ("DAI", "USD"),
    "ENJ": ("ENJ", "USD"),
    "KNC": ("KNC", "USD"),
    "LINK": ("LINK", "USD"),
    "MANA": ("MANA", "USD"),
    "MKR": ("MKR", "USD"),
    "REN": ("REN", "USD"),
    "SNX": ("SNX", "USD"),
    "sUSD": ("SUSD", "USD"),
    "TUSD": ("TUSD", "USD"),
    "USDC": ("USDC", "USD"),
    "CRV": ("CRV", "USD"),
    "GUSD": None,
    "BAL": ("BAL", "ETH"),
    "xSUSHI": None,
    "renFIL": ("FIL", "USD"),
    "RAI": ("RAI", "USD"),
    "AMPL": ("AMPL", "USD"),
    "USDP": ("PAX", "ETH"),  # USDP was formerly branded Paxos Standard (PAX)
    "DPI": ("DPI", "USD"),
    "FRAX": ("FRAX", "USD"),
    "FEI": ("FEI", "USD"),
    "stETH": None,
    "ENS": None,
    "UST": ("UST", "USD"),
    "CVX": None,
    "1INCH": ("1INCH", "USD"),
    "LUSD": ("LUSD", "USD"),
}


def _normalize_description(desc: str | None) -> tuple[str, str] | None:
    if not desc:
        return None
    parts = [p.strip().upper() for p in desc.split("/")]
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def main() -> None:
    describe_script = _HERE / "describe_chainlink_aggregators.py"
    result = subprocess.run(
        [sys.executable, str(describe_script)],
        check=True,
        capture_output=True,
        text=True,
    )
    descriptions: dict[str, dict] = json.loads(result.stdout)

    by_key: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
    for addr, info in descriptions.items():
        key = _normalize_description(info.get("description"))
        if key is None:
            continue
        by_key.setdefault(key, []).append((addr, info["rows"], info["decimals"]))

    print("RESERVE_CHAINLINK_FEEDS: dict[str, dict] = {")
    for address, (symbol, *_rest) in ONCHAIN_RESERVE_CONFIG.items():
        alias = SYMBOL_ALIAS.get(symbol)
        if alias is None:
            continue
        matches = by_key.get(alias)
        if not matches:
            continue
        decimals = sorted({m[2] for m in matches})[0]
        aggs = sorted(matches, key=lambda m: -m[1])
        agg_list = ", ".join(f'"{a}"' for a, _, _ in aggs)
        total_rows = sum(m[1] for m in matches)
        print(
            f'    "{address}": {{"symbol": "{symbol}", "quote_symbol": '
            f'"{alias[0]}", "quote": "{alias[1]}", "decimals": {decimals}, '
            f'"aggregators": [{agg_list}]}},  # {total_rows} rows'
        )
    print("}")
    print()
    print("UNCOVERED_RESERVES = {")
    for address, (symbol, *_rest) in ONCHAIN_RESERVE_CONFIG.items():
        if SYMBOL_ALIAS.get(symbol) is None:
            print(f'    "{address}": "{symbol}",')
    print("}")
    print()
    eth_usd = sorted(by_key.get(("ETH", "USD"), []), key=lambda m: -m[1])
    print("ETH_USD_AGGREGATORS = [")
    for addr, rows, dec in eth_usd:
        print(f'    "{addr}",  # {rows} rows, decimals={dec}')
    print("]")


if __name__ == "__main__":
    main()
