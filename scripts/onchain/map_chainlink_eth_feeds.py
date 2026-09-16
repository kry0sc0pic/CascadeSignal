"""Build the reserve -> Chainlink asset/ETH feed map for `chainlink_feeds.py` (CAS-28 H1).

Companion to `map_chainlink_reserves.py`, which built `RESERVE_CHAINLINK_FEEDS`
(asset/USD, with a couple of ETH-quoted exceptions converted to USD). This
script instead looks for each reserve's *native* asset/ETH feed -- the one
Aave v2's `calculateUserAccountData` actually reads (WETH is the numeraire,
so every other reserve is priced directly in ETH on-chain, never via two
independent USD feeds divided against each other).

Reuses the exact same building block as `map_chainlink_reserves.py`:
`describe_chainlink_aggregators.py` fetches `description()`/`decimals()` for
the 392+ aggregator addresses that emitted real `AnswerUpdated` events in the
already-pulled golden-episode data (`data/raw/chainlink/chain=1/`). That pull
was never filtered to specific aggregator addresses, so it already contains
whichever asset/ETH feeds were live during those windows -- no new on-chain
data pull needed, just a different `description()` match (`"<SYMBOL> / ETH"`
instead of `"<SYMBOL> / USD"`).

30 of Aave v2's 37 reserves have a matching raw asset/ETH feed among the
pulled aggregators; WETH is the numeraire identity (price_ETH(WETH) == 1.0
by construction, not a feed lookup) so it is handled specially by
`state.prices.EthNumeraire` rather than listed here. The remaining 6 --
GUSD, xSUSHI, stETH, ENS, CVX (no feed of either quote in the pull) and LUSD
(has a USD feed but no ETH-quoted one) -- have no entry; `EthNumeraire.price_at`
returns `None` for them, same no-coverage contract as `ChainlinkPriceOracle`.

This script's output was manually reviewed and copied into
`chainlink_feeds.py` (same convention as `map_chainlink_reserves.py`) --
re-run it if the Chainlink pull is ever extended to new block ranges.

Usage:
    python scripts/onchain/map_chainlink_eth_feeds.py
Prints the `RESERVE_CHAINLINK_ETH_FEEDS` / `UNCOVERED_ETH_RESERVES` Python
literals to stdout.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from cascadesignal.state.reserves import ONCHAIN_RESERVE_CONFIG  # noqa: E402

_HERE = Path(__file__).resolve().parent

# symbol -> the quote_symbol to look for in a "<quote_symbol> / ETH"
# description. `None` means don't look (WETH is the numeraire identity,
# handled specially by EthNumeraire; GUSD/xSUSHI/stETH/ENS/CVX are known to
# have no raw feed of any quote in the pull -- see map_chainlink_reserves.py).
ETH_SYMBOL_ALIAS: dict[str, str | None] = {
    "USDT": "USDT",
    "WBTC": "BTC",  # same WBTC->BTC approximation as the USD-side map
    "WETH": None,  # numeraire identity -- not a feed lookup
    "YFI": "YFI",
    "ZRX": "ZRX",
    "UNI": "UNI",
    "AAVE": "AAVE",
    "BAT": "BAT",
    "BUSD": "BUSD",
    "DAI": "DAI",
    "ENJ": "ENJ",
    "KNC": "KNC",
    "LINK": "LINK",
    "MANA": "MANA",
    "MKR": "MKR",
    "REN": "REN",
    "SNX": "SNX",
    "sUSD": "SUSD",
    "TUSD": "TUSD",
    "USDC": "USDC",
    "CRV": "CRV",
    "GUSD": None,
    "BAL": "BAL",
    "xSUSHI": None,
    "renFIL": "FIL",
    "RAI": "RAI",
    "AMPL": "AMPL",
    "USDP": "PAX",  # USDP was formerly branded Paxos Standard (PAX)
    "DPI": "DPI",
    "FRAX": "FRAX",
    "FEI": "FEI",
    "stETH": None,
    "ENS": None,
    "UST": "UST",
    "CVX": None,
    "1INCH": "1INCH",
    "LUSD": None,  # has a USD feed (RESERVE_CHAINLINK_FEEDS) but no ETH-quoted one in the pull
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

    print("RESERVE_CHAINLINK_ETH_FEEDS: dict[str, dict] = {")
    for address, (symbol, *_rest) in ONCHAIN_RESERVE_CONFIG.items():
        if symbol == "WETH":
            continue
        alias = ETH_SYMBOL_ALIAS.get(symbol)
        if alias is None:
            continue
        matches = by_key.get((alias, "ETH"))
        if not matches:
            continue
        decimals = sorted({m[2] for m in matches})[0]
        aggs = sorted(matches, key=lambda m: -m[1])
        agg_list = ", ".join(f'"{a}"' for a, _, _ in aggs)
        total_rows = sum(m[1] for m in matches)
        print(
            f'    "{address}": {{"symbol": "{symbol}", "quote_symbol": '
            f'"{alias}", "decimals": {decimals}, '
            f'"aggregators": [{agg_list}]}},  # {total_rows} rows'
        )
    print("}")
    print()
    print("UNCOVERED_ETH_RESERVES = {")
    for address, (symbol, *_rest) in ONCHAIN_RESERVE_CONFIG.items():
        if symbol == "WETH":
            continue
        alias = ETH_SYMBOL_ALIAS.get(symbol)
        if alias is not None and by_key.get((alias, "ETH")):
            continue
        print(f'    "{address}": "{symbol}",')
    print("}")


if __name__ == "__main__":
    main()
