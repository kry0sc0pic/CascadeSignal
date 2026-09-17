"""DefiLlama historical token-price ingestion.

DefiLlama's `coins.llama.fi` price API is free (no key) and serves daily USD
price series for arbitrary ERC-20 tokens keyed by `ethereum:<address>`. We pull
the core collateral / debt assets that appear across Aave v2/v3, Compound v2/v3
and Maker on Ethereum mainnet over the study period (2021-01 → 2026-02).

This is a covariate time-series, not an on-chain event stream, so it does NOT
use the canonical event schema. It serves two purposes downstream:

 1. USD enrichment of raw token amounts for protocols that lack it in their
 decoded parquet (Compound v2/v3, Maker).
 2. A price-feature substitute for the Chainlink oracle feed (blocked on Dune
 credits) — DefiLlama prices are oracle-adjacent, daily granularity.

The `/chart` endpoint caps a single response at 500 data points
(coins x timestamps), so we paginate per token in windows of `SPAN` days.

Output:
 data/raw/defillama/prices/token_prices_daily.parquet
 data/raw/defillama/prices/coverage.json
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

log = logging.getLogger(__name__)

COINS_BASE = "https://coins.llama.fi"

# DefiLlama caps `/chart` at 500 points per response (coins x timestamps).
# One coin per request => up to 500 daily points per window.
SPAN = 450
_DAY = 86_400

# Study period: ~Jan 1 2021 → ~Feb 28 2026 (aligns with configs/ingest.yaml).
DEFAULT_START = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp)
DEFAULT_END = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp)

# Core Ethereum-mainnet lending-market assets (collateral + debt) across
# Aave v2/v3, Compound v2/v3, Maker. Addresses are checksummed mainnet ERC-20s.
TOKENS: dict[str, str] = {
 # Majors / blue-chip collateral
 "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
 "WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
 # LSTs (heavy Aave v3 / cascade-relevant collateral)
 "stETH": "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84",
 "wstETH": "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0",
 "cbETH": "0xBe9895146f7AF43049ca1c1AE358B0541Ea49704",
 "rETH": "0xae78736Cd615f374D3085123A210448E74Fc6393",
 "weETH": "0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee",
 # Stablecoins (debt + collateral)
 "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
 "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
 "DAI": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
 "FRAX": "0x853d955aCEf822Db058eb8505911ED77F175b99e",
 "LUSD": "0x5f98805A4E8be255a32880FDeC7F6728C6568bA0",
 "sDAI": "0x83F20F44975D03b1b09e64809B757c47f942BEeA",
 "crvUSD": "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E",
 "GUSD": "0x056Fd409E1d7A124BD7017459dFEa2F387b6d5Cd",
 "USDe": "0x4c9EDD5852cd905f086C759E8383e09bff1E68B3",
 "TUSD": "0x0000000000085d4780B73119b644AE5ecd22b376",
 # Volatile governance / long-tail collateral
 "LINK": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
 "AAVE": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",
 "UNI": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
 "MKR": "0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2",
 "COMP": "0xc00e94Cb662C3520282E6f5717214004A7f26888",
 "SNX": "0xC011a73ee8576Fb46F5E1c5751cA3B9Fe0af2a6F",
 "YFI": "0x0bc529c00C6401aEF6D220BE8C6Ea1667F6Ad93e",
}


def _coin_key(address: str) -> str:
 return f"ethereum:{address}"


class DefiLlamaPriceIngester:
 """Pulls daily USD token prices from DefiLlama's free coins API."""

 def __init__(
 self,
 data_dir: Path = Path("data/raw"),
 sleep_s: float = 0.2,
 ):
 self.out_dir = Path(data_dir) / "defillama" / "prices"
 self.out_dir.mkdir(parents=True, exist_ok=True)
 self.sleep_s = sleep_s
 self._session = requests.Session
 self._session.headers.update({"User-Agent": "CascadeSignal/0.1"})

 def fetch_token_series(
 self,
 symbol: str,
 address: str,
 start_ts: int,
 end_ts: int,
 ) -> pd.DataFrame:
 """Fetch a full daily price series for one token, paginating windows."""
 coin_key = _coin_key(address)
 rows: list[dict] = []
 cursor = start_ts
 while cursor < end_ts:
 span = min(SPAN, (end_ts - cursor) // _DAY + 1)
 url = f"{COINS_BASE}/chart/{coin_key}"
 params: dict[str, int | str] = {
 "start": cursor,
 "span": span,
 "period": "1d",
 }
 try:
 resp = self._session.get(url, params=params, timeout=60)
 resp.raise_for_status
 payload = resp.json
 except requests.RequestException as exc:
 log.warning("%s: request failed at cursor %d: %s", symbol, cursor, exc)
 break

 coin = payload.get("coins", {}).get(coin_key)
 points = (coin or {}).get("prices", [])
 if not points:
 # No data in this window (token not yet deployed / no coverage).
 cursor += SPAN * _DAY
 time.sleep(self.sleep_s)
 continue

 for p in points:
 rows.append(
 {
 "symbol": symbol,
 "address": address.lower,
 "coin_key": coin_key,
 "timestamp": p["timestamp"],
 "price": p.get("price"),
 "confidence": p.get("confidence"),
 }
 )
 # Advance past the last returned point to avoid re-fetching.
 last_ts = points[-1]["timestamp"]
 cursor = max(last_ts + _DAY, cursor + _DAY)
 time.sleep(self.sleep_s)

 if not rows:
 return pd.DataFrame(
 columns=[
 "symbol",
 "address",
 "coin_key",
 "timestamp",
 "price",
 "confidence",
 ]
 )
 df = pd.DataFrame(rows).drop_duplicates(subset=["coin_key", "timestamp"])
 return df.sort_values("timestamp").reset_index(drop=True)

 def ingest(
 self,
 tokens: Optional[dict[str, str]] = None,
 start_ts: int = DEFAULT_START,
 end_ts: int = DEFAULT_END,
 ) -> pd.DataFrame:
 """Pull all tokens, write a single long parquet + coverage summary."""
 tokens = tokens or TOKENS
 frames: list[pd.DataFrame] = []
 coverage: list[dict] = []

 for symbol, address in tokens.items:
 log.info("Fetching DefiLlama prices for %s (%s)", symbol, address)
 df = self.fetch_token_series(symbol, address, start_ts, end_ts)
 if df.empty:
 log.warning(" no price data for %s", symbol)
 coverage.append(
 {"symbol": symbol, "address": address.lower, "rows": 0}
 )
 continue
 frames.append(df)
 coverage.append(
 {
 "symbol": symbol,
 "address": address.lower,
 "rows": int(len(df)),
 "first_ts": int(df["timestamp"].min),
 "last_ts": int(df["timestamp"].max),
 "first_date": _iso(df["timestamp"].min),
 "last_date": _iso(df["timestamp"].max),
 }
 )
 log.info(
 " %d daily points (%s → %s)",
 len(df),
 _iso(df["timestamp"].min),
 _iso(df["timestamp"].max),
 )

 if not frames:
 raise RuntimeError("No DefiLlama price data returned for any token")

 combined = pd.concat(frames, ignore_index=True)
 combined["date"] = pd.to_datetime(combined["timestamp"], unit="s", utc=True)
 combined = combined[
 [
 "symbol",
 "address",
 "coin_key",
 "timestamp",
 "date",
 "price",
 "confidence",
 ]
 ]

 out_path = self.out_dir / "token_prices_daily.parquet"
 combined.to_parquet(out_path, index=False)
 log.info(
 "Wrote %d rows across %d tokens → %s", len(combined), len(frames), out_path
 )

 (self.out_dir / "coverage.json").write_text(
 json.dumps(
 {
 "source": "coins.llama.fi/chart",
 "pulled_at": datetime.now(timezone.utc).isoformat,
 "start_ts": start_ts,
 "end_ts": end_ts,
 "total_rows": int(len(combined)),
 "tokens": coverage,
 },
 indent=2,
 )
 )
 return combined


def _iso(ts: int) -> str:
 return datetime.fromtimestamp(int(ts), tz=timezone.utc).date.isoformat
